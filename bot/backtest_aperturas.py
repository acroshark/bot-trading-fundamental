"""Backtest v2 del motor de aperturas (bot/apertura.py) con velas de 1 min de Binance al contado.

Cada apertura real de cada bolsa (horas reales con cambio de hora, sin fines de semana ni festivos) y cada moneda se
simula con las MISMAS funciones puras del motor en vivo: CoinRun (rango, confirmación y bandera MOVING), features,
entry_plan (puerta R:R desde la entrada real), simulate_plan (salidas como el paper), gate_verdict (puerta de
probabilidades) y day_limits (límites diarios). Lo simulado y lo vivo coinciden.

- Datos: archivos mensuales públicos de data.binance.vision (BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT; el precio en USDT es
  la aproximación al USDC de OKX EU) en config.HIST_DIR/1m, y el mes en curso con la API de klines.
- Costes: config.FEE_PCT por lado más 2 pb de deslizamiento por lado.
- Sin mirar el futuro: la entrada es la apertura de la vela siguiente a la que confirma y el R:R se mide desde ahí.
  Dentro de cada vela, supuesto pesimista: primero el stop; luego parcial y objetivo; luego el extremo favorable mueve
  el stop y, si el contrario toca el stop nuevo, sale en él.
- v2: una entrada por moneda y ventana, una sola posición por moneda (entre ventanas solapadas, por orden de entrada
  como en vivo), bandera MOVING, límites diarios (tope de entradas y riesgo comprometido) y puerta de probabilidades en
  avance (solo con las sesiones anteriores de la misma ventana).
- Rejilla de salidas por etapas: etapa 1 con las entradas de la v1 (rango 30 min, rvol 2) y 144 salidas
  (stop × break-even × objetivo × parcial × confirmación); etapa 2 afina rango y rvol con la mejor salida. Todo se
  elige SOLO con el 70 % inicial de las aperturas (corte en la medianoche de Madrid); el 30 % final queda fuera.
- Variantes de señal: «ruptura libre», «encadenamiento» y «oráculo» (TECHO INALCANZABLE).
- Simulación diaria de la cuenta (10.000 USDC, 0,5 % de riesgo en el stop, tope de nominal del 40 %) a 1:1 y a 2:1.

Uso: python -m bot.backtest_aperturas [--meses 24] [--offline] [--procesos 4] [--salida archivo.json]
Escribe config.BACKTEST_DIR/aperturas.json (claves de la v1 más exits, gate, daily y trades_sample) solo con la pasada
completa (24 meses o más y con red); las pasadas cortas o sin red, en aperturas-<meses>m[-offline].json (out_name)."""
import argparse
import array
import asyncio
import bisect
import hashlib
import io
import itertools
import json
import logging
import math
import os
import statistics
import sys
import time
import zipfile
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

from . import apertura, comisiones, config, history, paper, regions
from .apertura import C, HI, LO, MIN, O, PRE_MIN, T, V
from .models import now_ms

log = logging.getLogger("backtest_aperturas")

ZIP_URL = "https://data.binance.vision/data/spot/monthly/klines/{sym}/1m/{sym}-1m-{ym}.zip"
API_URL = "https://data-api.binance.vision/api/v3/klines"
SYMS = {c: f"{c}USDT" for c in config.COINS}
SLIP_BPS = 2.0
OOS_SHARE = 0.30
TARGET_PCT = 3.0
COST_RT_PCT = 2 * (config.FEE_PCT + SLIP_BPS / 100)   # coste de ida y vuelta en % (0,24 %)
MIN_TRADES_SELECT = 100      # una combinación con menos operaciones en la muestra no se puede elegir
MIN_TRADES_GATE = 50         # la puerta solo se activa si en la muestra deja al menos estas operaciones
PART_MAX_AGE_S = 3600        # el mes en curso se vuelve a pedir si el archivo tiene más de 1 h
CHAIN_MARGIN_MS = 5 * MIN    # el cierre de una bolsa se usa 5 min después (subastas y retrasos de Yahoo)
CHAIN_PREF = ["nuevayork", "londres", "frankfurt", "tokio", "hongkong", "shanghai", "seul", "madrid", "saopaulo"]
COVERAGE = 0.9               # aperturas con menos del 90 % de las velas de la sesión no se simulan
MAD = ZoneInfo("Europe/Madrid")
VERSION = 2
VARIANTS = {
    "libre": {"name": "Ruptura libre", "label": "Ruptura libre: entra hacia el lado que rompa, sin señal", "ceiling": False},
    "encadenamiento": {"name": "Encadenamiento",
                       "label": "Encadenamiento: signo de la sesión anterior de la bolsa que cerró justo antes "
                                "(o del retorno cripto de los 60 min previos)", "ceiling": False},
    "oraculo": {"name": "Oráculo (TECHO INALCANZABLE)",
                "label": "Oráculo: signo real de las 4 h siguientes. TECHO INALCANZABLE: muestra cuánto daría la "
                         "estrategia si las noticias acertaran siempre; no es operable", "ceiling": True},
}
VKEYS = list(VARIANTS)
REC_VARIANT = "encadenamiento"   # la recomendación sale de esta variante (la señal en vivo también es direccional)
FOCUS = {"windows": ["nuevayork"], "coins": ["SOL", "ETH", "XRP"]}
NY_LOW_SHARE = 0.15              # el veredicto explica por qué Nueva York opera poco solo por debajo de esta cuota

# Rejilla de salidas (etapa 1). tp1_r: la parcial va a +1 R con objetivo fijo y a +2 R sin tope (mitad a 2R y el resto
# con stop dinámico de 3 ATR de 5 min, como propone la auditoría del paquete day-trader-skill).
TARGETS = {"2R": {"target_r": 2.0, "target_pct": None, "trail_atr": 0.0, "tp1_r": 1.0},
           "3R": {"target_r": 3.0, "target_pct": None, "trail_atr": 0.0, "tp1_r": 1.0},
           "3pct": {"target_r": None, "target_pct": 3.0, "trail_atr": 0.0, "tp1_r": 1.0},
           "trail": {"target_r": None, "target_pct": None, "trail_atr": 3.0, "trail_tf": 5, "tp1_r": 2.0}}
EXIT_GRID = {"stop_mode": ["or_mid", "or_far", "atr5"], "be_r": [None, 1.5, 2.0], "target": list(TARGETS),
             "tp1_frac": [0.0, 0.5], "confirm_tf": [1, 5]}
ENTRY_GRID = {"range_min": [15, 30], "rvol_min": [1.5, 2.0]}   # etapa 2, con la mejor salida
# El preset V2 de apertura.py tal cual (medio del rango, 2R, sin break-even, 5 min, rango 30, rvol 1,5 de PARAMS).
REF_V2 = {"stop_mode": "or_mid", "be_r": None, "target": "2R", "tp1_frac": 0.0, "confirm_tf": 5, "range_min": 30,
          "rvol_min": 1.5}
# Base: preset V2 del motor (stop con suelo de costes, sin reentradas, MOVING, límites diarios) con las entradas de la
# v1. La puerta se aplica aparte (con y sin).
BASE = {**apertura.PARAMS, **apertura.V2, "range_min": 30, "rvol_min": 2.0, "gate": False}
REC_KEYS = sorted(set(apertura.V2) | {"range_min", "rvol_min", "stop_min_pct", "stop_atr5", "tp1_r", "trail_atr",
                                      "trail_tf", "target_r", "target_pct", "be_r", "tp1_frac", "confirm_tf",
                                      "stop_mode", "gate"})
EXIT_ONLY = ("be_r", "tp1_frac", "tp1_r", "trail_start_r", "gate", "gate_pp", "gate_min_n",
             "gate_sessions")   # no cambian la entrada: comparten CoinRun
# Historia de la puerta: 60 sesiones (preset V2) y ~1 año. Con 60 casi nunca hay 20 casos decididos.
GATE_SESSIONS = (60, 250)
STOP_TEXT = {"or_mid": "stop en el medio del rango", "or_far": "stop en el otro lado del rango",
             "atr5": "stop a 1,5 ATR de 5 min"}
TARGET_TEXT = {"2R": "objetivo 2R", "3R": "objetivo 3R", "3pct": "objetivo +3 % neto",
               "trail": "sin tope, con stop dinámico de 3 ATR de 5 min"}

# Operaciones: filas de números en array("d") (compactas para pasar entre procesos).
COLS = ("open_ms", "wid", "coin", "dir", "entry_t", "exit_t", "ret", "R", "stop", "rr", "why", "partial", "mfe", "mae",
        "mfe_win", "mae_win", "hit3", "gate_ok", "gate_edge", "gate_dec")
NC = len(COLS)
(I_OPEN, I_WID, I_COIN, I_DIR, I_ENTRY, I_EXIT, I_RET, I_R, I_STOP, I_RR, I_WHY, I_PART, I_MFE, I_MAE, I_MFEW, I_MAEW,
 I_HIT3, I_GOK, I_GEDGE, I_GDEC) = range(NC)
WHYS = ("inicial", "be", "dinamico", "objetivo", "tiempo", "fin de datos")
WHY_CODE = {w: k for k, w in enumerate(WHYS)}
WHY_TEXT = {"inicial": "stop inicial", "be": "break-even", "dinamico": "stop dinámico", "objetivo": "objetivo",
            "tiempo": "tiempo (fin de la ventana)", "fin de datos": "fin de datos"}
FIRST = {0: "stop", 1: "stop", 2: "stop", 3: "objetivo", 4: "ninguno", 5: "ninguno"}
BUSY_WHY = "ya hay una posición abierta en la moneda (otra ventana)"


def _num(x: float, d: int = 2) -> str:
    return f"{x:.{d}f}".replace(".", ",")


def _sg(x: float, d: int = 2) -> str:
    return ("+" if x >= 0 else "") + _num(x, d)


def _pc(x: float, d: int = 0) -> str:
    return _num(x * 100, d) + " %"


# ---------------------------------------------------------------- descarga y caché

def dir_1m() -> Path:
    return config.HIST_DIR / "1m"


def zip_path(sym: str, ym: str) -> Path:
    return dir_1m() / f"{sym}-1m-{ym}.zip"


def part_path(sym: str, ym: str) -> Path:
    return dir_1m() / f"{sym}-1m-{ym}-api.json"


def month_list(today: date, n: int) -> tuple[list[str], str]:
    """Los n meses completos anteriores al mes de today (del más antiguo al más reciente) y el mes en curso."""
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        out.append(f"{y:04d}-{m:02d}")
    return out[::-1], f"{today.year:04d}-{today.month:02d}"


def month_bounds(ym: str) -> tuple[int, int]:
    y, m = int(ym[:4]), int(ym[5:])
    a = datetime(y, m, 1, tzinfo=timezone.utc)
    b = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=timezone.utc)
    return int(a.timestamp() * 1000), int(b.timestamp() * 1000)


def norm_ms(t: int) -> int:
    """Desde 2025 algunos archivos de Binance dan el tiempo en microsegundos: se pasa a milisegundos."""
    return t // 1000 if t > 10 ** 14 else t


def parse_csv_line(line: str) -> list | None:
    """Fila del CSV de Binance → vela [open_ms, o, h, l, c, vol_base, vol_quote, taker_buy_base] (None si es cabecera)."""
    p = line.split(",")
    if len(p) < 10 or not p[0].strip().isdigit():
        return None
    return [norm_ms(int(p[0])), float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5]), float(p[7]), float(p[9])]


def zip_ok(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as z:
            return bool(z.namelist())
    except (zipfile.BadZipFile, OSError):
        return False


async def _download(session: aiohttp.ClientSession, url: str, tries: int = 4) -> bytes | None:
    """Descarga con reintentos y espera creciente; None si no existe (404). Comprueba el tamaño anunciado."""
    wait = 2.0
    for k in range(tries):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=300)) as r:
                if r.status == 404:
                    return None
                r.raise_for_status()
                body = await r.read()
                size = r.headers.get("Content-Length")
                if size is not None and int(size) != len(body):
                    raise IOError(f"tamaño recibido {len(body)} distinto del anunciado {size}")
                return body
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if k == tries - 1:
                raise
            log.info("Reintento %s en %.0f s: %s", url, wait, str(e)[:120])
            await asyncio.sleep(wait)
            wait *= 2
    return None


async def fetch_zip(session: aiohttp.ClientSession, sym: str, ym: str) -> bool:
    """Archivo mensual verificado (tamaño, suma SHA-256 publicada y zip legible). False si Binance no lo tiene."""
    url = ZIP_URL.format(sym=sym, ym=ym)
    body = await _download(session, url)
    if body is None:
        return False
    try:
        cs = await _download(session, url + ".CHECKSUM", tries=2)
    except Exception as e:
        log.info("Sin suma de control de %s: %s", url, e)
        cs = None
    if cs:
        want = cs.decode("ascii", "ignore").split()[0].lower()
        got = hashlib.sha256(body).hexdigest()
        if want != got:
            raise IOError(f"suma SHA-256 distinta en {sym} {ym}")
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        if z.testzip() is not None:
            raise IOError(f"zip dañado {sym} {ym}")
    path = zip_path(sym, ym)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(body)
    os.replace(tmp, path)
    return True


async def fetch_api(session: aiohttp.ClientSession, sym: str, start: int, end: int, now: int) -> list[list]:
    """Velas cerradas de [start, end) con la API pública de klines, de 1000 en 1000."""
    rows, t = [], start
    while t < min(end, now):
        raw = await history._get_json(session, API_URL, {"symbol": sym, "interval": "1m", "startTime": t, "limit": 1000})
        if not raw:
            break
        rows.extend(b for b in apertura.to_bars(raw, now) if b[T] < end)
        last = int(raw[-1][0])
        if len(raw) < 1000:
            break
        t = last + MIN
        await asyncio.sleep(0.1)
    return rows


async def _api_month(session, sym: str, ym: str, now: int) -> int:
    path = part_path(sym, ym)
    a, b = month_bounds(ym)
    old = history._read(path) or {}
    rows = {r[0]: r for r in old.get("rows") or []}
    start = max(rows) + MIN if rows else a
    for r in await fetch_api(session, sym, start, b, now):
        rows[r[0]] = r
    out = [rows[t] for t in sorted(rows)]
    history._write(path, {"symbol": sym, "month": ym, "fetched": now, "rows": out})
    return len(out)


async def ensure_data(months: list[str], current: str, offline: bool) -> dict:
    """Descarga lo que falte. Devuelve {coin: [rutas]} y los errores. Un fallo de un archivo no para los demás."""
    files: dict[str, list[Path]] = {c: [] for c in config.COINS}
    errors: list[str] = []
    dir_1m().mkdir(parents=True, exist_ok=True)
    jobs = []
    for coin, sym in SYMS.items():
        for ym in months:
            z = zip_path(sym, ym)
            if z.exists() and not zip_ok(z):
                log.warning("Zip ilegible, se descarga de nuevo: %s", z)
                z.unlink()
            if not z.exists() and not offline:
                jobs.append((coin, sym, ym, "zip"))
        part = part_path(sym, current)
        fresh = part.exists() and time.time() - part.stat().st_mtime < PART_MAX_AGE_S
        if not offline and not fresh:
            jobs.append((coin, sym, current, "api"))
    if jobs:
        log.info("Descargando %d archivos de velas de 1 min…", len(jobs))
        sem = asyncio.Semaphore(4)
        now = now_ms()
        async with aiohttp.ClientSession(headers={"User-Agent": config.USER_AGENT}) as session:
            async def one(coin, sym, ym, kind):
                async with sem:
                    try:
                        if kind == "zip":
                            if not await fetch_zip(session, sym, ym):   # aún no publicado: el mes por la API
                                log.info("%s %s sin archivo mensual: se pide a la API", sym, ym)
                                await _api_month(session, sym, ym, now)
                        else:
                            await _api_month(session, sym, ym, now)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.warning("Velas %s %s: %s", sym, ym, e)
                        errors.append(f"{sym} {ym}: {str(e)[:160]}")
            await asyncio.gather(*(one(*j) for j in jobs))
    for coin, sym in SYMS.items():
        for ym in months + [current]:
            z, p = zip_path(sym, ym), part_path(sym, ym)
            if z.exists():
                files[coin].append(z)
            elif p.exists():
                files[coin].append(p)
            elif ym != current:
                errors.append(f"{sym} {ym}: sin datos")
    return {"files": files, "errors": errors}


# ---------------------------------------------------------------- serie en memoria (columnas compactas)

class Series:
    """Velas de 1 min de una moneda en columnas (array) con suma acumulada del volumen."""

    def __init__(self):
        self.t = array.array("q")
        self.cols = [array.array("d") for _ in range(7)]   # o, h, l, c, vol_base, vol_quote, taker_buy_base
        self.cv = array.array("d", [0.0])

    def add(self, row: list) -> None:
        t = row[0]
        if self.t and t <= self.t[-1]:
            return
        self.t.append(t)
        for k in range(7):
            self.cols[k].append(row[k + 1])
        self.cv.append(self.cv[-1] + row[V])

    def __len__(self) -> int:
        return len(self.t)

    def idx(self, t: int) -> int:
        return bisect.bisect_left(self.t, t)

    def vol_sum(self, a: int, b: int) -> float:
        """Volumen de las velas que abren en [a, b) (lo mismo que apertura.slot_volume)."""
        return self.cv[self.idx(b)] - self.cv[self.idx(a)]

    def slice(self, a: int, b: int) -> list[list]:
        i, j = self.idx(a), self.idx(b)
        o, h, l, c, v, qv, tb = self.cols
        return [[self.t[k], o[k], h[k], l[k], c[k], v[k], qv[k], tb[k]] for k in range(i, j)]

    def close_before(self, t: int) -> float | None:
        """Cierre de la última vela que abre antes de t."""
        i = self.idx(t) - 1
        return self.cols[3][i] if i >= 0 else None


def load_series(paths: list) -> Series:
    s = Series()
    for path in sorted(paths, key=lambda p: Path(p).name):
        path = Path(path)
        if path.suffix == ".zip":
            with zipfile.ZipFile(path) as z:
                with z.open(z.namelist()[0]) as f:
                    for line in io.TextIOWrapper(f, encoding="ascii", errors="ignore"):
                        r = parse_csv_line(line)
                        if r:
                            s.add(r)
        else:
            for r in (history._read(path) or {}).get("rows") or []:
                s.add(r)
    return s


# ---------------------------------------------------------------- aperturas, festivos y encadenamiento

def index_data() -> dict[str, dict]:
    """Índices diarios que cachea history.py: {bolsa: {"close": {fecha: (cierre, cierre anterior)}, "first", "last"}}."""
    out = {}
    for k, m in regions.MARKETS.items():
        rows = history.load_index(k) if m.index else []
        if not rows:
            continue
        closes, prev = {}, None
        for r in rows:
            if prev is not None and r[4]:
                closes[r[0]] = (r[4], prev)
            prev = r[4] or prev
        out[k] = {"close": closes, "first": rows[0][0], "last": rows[-1][0]}
    return out


def _trades_on(idx: dict, key: str, d: date) -> bool:
    x = idx.get(key)
    if not x or not (x["first"] <= d.isoformat() <= x["last"]):
        return True   # sin datos de ese día no se sabe: se supone que abre
    return d.isoformat() in x["close"]


def build_openings(start: date, end: date, idx: dict) -> list[dict]:
    """Aperturas reales entre start y end: días laborables, sin los festivos que delata el histórico de índices
    (una ventana cuenta si al menos una de sus bolsas abre)."""
    out, d = [], start
    while d <= end:
        for o in apertura.openings_on(d):
            if any(_trades_on(idx, k, d) for k in o["markets"]):
                out.append(o)
        d += timedelta(days=1)
    return sorted(out, key=lambda o: (o["open_ms"], o["id"]))


def chain_signal(o: dict, idx: dict) -> tuple[int, str] | None:
    """Signo de la última sesión de la bolsa que cerró justo antes de la apertura (cierre contra cierre anterior).
    None si alguna bolsa pudo cerrar después y no hay datos de ese día (se usa entonces el retorno cripto)."""
    open_ms = o["open_ms"]
    best = None
    for k in CHAIN_PREF:
        if k in o["markets"]:
            continue
        m = regions.MARKETS[k]
        x = idx.get(k)
        d = datetime.fromtimestamp(open_ms / 1000, ZoneInfo(m.tz)).date()
        for _ in range(10):
            close = int(regions.close_utc_on(m, d).timestamp() * 1000)
            if regions.is_trading_day(m, d) and close + CHAIN_MARGIN_MS <= open_ms:
                if not x or d.isoformat() > x["last"]:
                    return None   # pudo cerrar más tarde y no tenemos el dato
                if d.isoformat() in x["close"]:
                    c, pc = x["close"][d.isoformat()]
                    cand = (close, -CHAIN_PREF.index(k), k, c / pc - 1)
                    if best is None or cand[:2] > best[:2]:
                        best = cand
                    break
            d -= timedelta(days=1)
    if not best:
        return None
    ch = best[3]
    sign = 1 if ch > 0 else -1 if ch < 0 else 0
    return sign, f"{regions.MARKETS[best[2]].name} {_sg(ch * 100)} %"


# ---------------------------------------------------------------- simulación (mismas reglas que en vivo)

def simulate_trade(bars: list, j: int, d: int, stop_pct: float, trail: float, end_ms: int, fee: float,
                   slip: float) -> dict:
    """Salida de la v1 (break-even a +1 R en la entrada pelada y stop dinámico desde el principio), que se queda como
    referencia: apertura.simulate_plan con el plan de la v1 da lo mismo. Devuelve {k, t, ret (% neto), why}."""
    entry = bars[j][O] * (1 + d * slip)
    stop = entry * (1 - d * stop_pct / 100)
    best, be, kind = entry, False, "inicial"
    k, px, why = len(bars) - 1, bars[-1][C], "fin de datos"
    for k in range(j, len(bars)):
        b = bars[k]
        adv = b[LO] if d > 0 else b[HI]   # el extremo en contra de la vela
        if d * (adv - stop) <= 0:   # 1) el stop vigente, antes que nada (en la apertura si la vela ya abre peor)
            px = min(stop, b[O]) if d > 0 else max(stop, b[O])
            why = kind
            break
        fav = b[HI] if d > 0 else b[LO]
        if d * (fav - best) > 0:
            best = fav
        stop, be, mv = apertura.dynamic_stop(d, entry, stop_pct, trail, best, stop, be)
        kind = mv or kind
        if d * (adv - stop) <= 0:   # 2) pesimista: el extremo favorable llegó antes y el contrario tocó el stop nuevo
            px, why = stop, kind
            break
        if b[T] + MIN >= end_ms:   # 3) fin de la ventana
            px, why = b[C], "tiempo"
            break
    ex = px * (1 - d * slip)
    ret = (d * (ex / entry - 1) - fee * (1 + ex / entry)) * 100
    return {"k": k, "t": bars[k][T] + MIN, "ret": ret, "why": why}


def make_combo(stop_mode: str, be_r, target: str, tp1_frac: float, confirm_tf: int, range_min: int = 30,
               rvol_min: float = 2.0, gate_sessions: int = BASE["gate_sessions"]) -> dict:
    """Combinación de la rejilla: etiqueta corta y parámetros completos del motor (BASE con los cambios)."""
    lab = {"stop_mode": stop_mode, "be_r": be_r, "target": target, "tp1_frac": tp1_frac, "confirm_tf": confirm_tf,
           "range_min": range_min, "rvol_min": rvol_min, "gate_sessions": gate_sessions}
    p = {**BASE, **TARGETS[target], "stop_mode": stop_mode, "be_r": be_r, "tp1_frac": tp1_frac,
         "confirm_tf": confirm_tf, "range_min": range_min, "rvol_min": rvol_min, "gate_sessions": gate_sessions}
    return {"label": lab, "params": p}


def exit_combos() -> list[dict]:
    """Etapa 1: las 144 salidas con las entradas de la v1 (rango 30, rvol 2)."""
    return [make_combo(*vals) for vals in itertools.product(*EXIT_GRID.values())]


def combo_text(lab: dict) -> str:
    t = TARGETS[lab["target"]]
    be = "sin break-even" if lab["be_r"] is None else f"break-even (cubriendo costes) a +{_num(lab['be_r'], 1)} R"
    part = "sin parcial" if not lab["tp1_frac"] else f"mitad en +{_num(t['tp1_r'], 0)} R"
    return (f"{STOP_TEXT[lab['stop_mode']]} · {TARGET_TEXT[lab['target']]} · {be} · {part} · confirmación al cierre "
            f"de {lab['confirm_tf']} min · rango {lab['range_min']} min · rvol ≥ {_num(lab['rvol_min'], 1)}")


def entry_key(p: dict) -> tuple:
    """Lo que decide la entrada (todo menos break-even, parcial y arranque del stop dinámico)."""
    return tuple(sorted((k, repr(v)) for k, v in p.items() if k not in EXIT_ONLY))


def find_entry(coin: str, open_ms: int, bars: list, i0: int, i_end: int, feats: list, d: int, p: dict, slip: float,
               busy_until: int = 0) -> tuple | None:
    """Primera entrada de una moneda en una apertura con apertura.CoinRun (d = 0: ruptura libre), como
    apertura.session_case: entrada en la apertura de la vela siguiente y puerta R:R desde ahí (entry_plan). Si otra
    ventana tiene posición en la moneda hasta busy_until, la orden no se ejecuta y se sigue buscando (como en vivo).
    Devuelve (j, dir, act) o None."""
    run = apertura.CoinRun(coin, open_ms, d, p, d == 0)
    for i in range(i0, i_end):
        f = feats[i]
        if f is None:
            return None
        act = run.on_bar(bars, i, f)
        if run.done:
            return None
        if not act or act["action"] != "enter":
            continue
        if i + 1 >= len(bars):
            return None
        if bars[i + 1][T] < busy_until:
            run.on_rejected(act["t"], BUSY_WHY)
            continue
        dd = act["dir"]
        plan = apertura.entry_plan(dd, bars[i + 1][O] * (1 + dd * slip), act, p)
        if not plan["ok"]:
            run.on_rejected(act["t"], plan["why"])
            continue
        return i + 1, dd, act
    return None


def window_excursion(bars: list, j: int, i_end: int, d: int, entry: float, stop_pct: float) -> tuple:
    """Desde la entrada hasta el fin de la ventana, sin salidas: MFE y MAE (%) y si llegó a +3 % neto antes de tocar el
    stop inicial (1/0; una vela que toca los dos cuenta como stop)."""
    stop = entry * (1 - d * stop_pct / 100)
    lvl = entry * (1 + d * (TARGET_PCT + COST_RT_PCT) / 100)   # los costes del backtest (config.FEE_PCT), no el perfil
    best = worst = entry
    hit, alive = 0, True
    for k in range(j, i_end):
        b = bars[k]
        adv, fav = (b[LO], b[HI]) if d > 0 else (b[HI], b[LO])
        if d * (fav - best) > 0:
            best = fav
        if d * (adv - worst) < 0:
            worst = adv
        if alive:
            if d * (adv - stop) <= 0:
                alive = False
            elif d * (fav - lvl) >= 0:
                hit, alive = 1, False
    return d * (best - entry) / entry * 100, d * (worst - entry) / entry * 100, hit


def record(bars: list, e: tuple, p: dict, i_end: int, end_ms: int, fee: float, slip: float, win=None) -> tuple:
    """Operación de una entrada con el plan de p (entry_plan + simulate_plan, como el paper). win: excursión de la
    ventana ya calculada (no depende de las salidas). Devuelve (registro, win)."""
    j, d, act = e
    entry = bars[j][O] * (1 + d * slip)
    plan = apertura.entry_plan(d, entry, act, p)
    res = apertura.simulate_plan(bars, j, d, plan, end_ms, fee, slip)
    if win is None:
        win = window_excursion(bars, j, i_end, d, entry, plan["stopPct"])
    rec = (d, bars[j][T], res["t"], res["ret"], res["R"], plan["stopPct"], plan["tp2R"] or 0.0, WHY_CODE[res["why"]],
           int(res["partial"]), res["mfe"], res["mae"], *win)
    return rec, win


def run_coin(args: tuple) -> dict:
    """Trabajo de un proceso: todas las aperturas de una moneda con todas las combinaciones y variantes. Devuelve las
    operaciones de cada combinación, variante y puerta (0 sin, 1 con) como array("d") de filas de COLS, y en «lost»
    las rupturas (apertura, ventana) que se perdieron porque otra ventana ocupaba la moneda.
    Una sola posición por moneda entre ventanas solapadas, por orden de ENTRADA como en vivo: entra la primera
    ruptura de cualquiera de ellas; las de las demás que llegan con la posición abierta se rechazan y se sigue
    buscando tras su salida (find_entry con busy_until).
    Con la comisión del backtest (fee, por lado) también en lo que mira el perfil de comisiones del bot (puerta R:R,
    suelo del stop, objetivo, regla del 3 % neto): comisiones.costes_de_backtest."""
    with comisiones.costes_de_backtest(args[6] * 100):
        return _run_coin(args)


def _run_coin(args: tuple) -> dict:
    coin, paths, openings, chain, combos, wids, fee, slip, with_gate, with_eff = args
    t0 = time.time()
    s = load_series(paths)
    cidx, widx = config.COINS.index(coin), {w: k for k, w in enumerate(wids)}
    groups: dict[tuple, list[int]] = {}
    for ci, p in enumerate(combos):
        groups.setdefault(entry_key(p), []).append(ci)
    p0 = combos[0]
    pre = max(apertura.pre_min(p) for p in combos)
    end_min = p0["session_max_min"]
    n_feat = max(p["first_entry_max_min"] for p in combos) + 2   # sin entrada en 90 min, CoinRun acaba
    fp = {**p0, "stop_mode": "atr5"}   # indicadores con el ATR de 5 min, que sirven a todas las combinaciones
    gates = (0, 1) if with_gate else (0,)
    busy = [[[0] * len(gates) for _ in VKEYS] for _ in combos]
    out = [[[array.array("d") for _ in gates] for _ in VKEYS] for _ in combos]
    lost = [[[array.array("d") for _ in gates] for _ in VKEYS] for _ in combos]
    hist: list[dict] = [{} for _ in combos]   # puerta: {(ventana, dir): casos de las sesiones anteriores}
    rows, eff = [], []
    cluster: list[dict] = []   # ventanas solapadas de la moneda que faltan por resolver
    cl_end = 0

    def remember(ci: int, wi: int, d: int, rec) -> None:
        case = None if rec is None else {"first": FIRST[rec[7]], "R": round(rec[4], 4), "tR": rec[6] or None}
        hist[ci].setdefault((wi, d), deque(maxlen=combos[ci]["gate_sessions"])).append(case)

    def gate_of(wi: int):
        gc: dict = {}

        def gate(ci: int, d: int) -> dict:   # solo con las sesiones anteriores de esta ventana
            if (ci, d) not in gc:
                h = hist[ci].get((wi, d)) or ()
                gc[(ci, d)] = apertura.gate_verdict([c for c in h if c], combos[ci], len(h))
            return gc[(ci, d)]
        return gate

    def resolve() -> None:
        """Cada variante, sin y con puerta, de las ventanas solapadas pendientes, por orden de entrada."""
        for ci, p in enumerate(combos):
            for vi in range(len(VKEYS)):
                for g in gates:
                    cands = []
                    for w in cluster:
                        dv = w["dirs"][vi]
                        if vi and not dv:   # sin señal no se opera (salvo la ruptura libre)
                            continue
                        if g and vi and not w["gate"](ci, dv)["ok"]:   # la puerta se mira antes de la apertura
                            continue
                        if w["recs"][ci][dv] is not None:
                            cands.append([w["recs"][ci][dv], w, dv])
                    while cands:
                        b = busy[ci][vi][g]
                        for c in cands:
                            if c[0][1] < b:   # entraba con la posición de otra ventana abierta: otra ruptura
                                w = c[1]
                                e = find_entry(coin, w["open_ms"], w["bars"], w["i0"], w["i_end"], w["feats"], c[2], p,
                                               slip, b)
                                c[0] = record(w["bars"], e, p, w["i_end"], w["end_ms"], fee, slip)[0] if e else None
                                if e is None:
                                    lost[ci][vi][g].extend((w["open_ms"], w["wi"]))
                        cands = [c for c in cands if c[0] is not None]
                        if not cands:
                            break
                        c = min(cands, key=lambda c: (c[0][1], c[1]["open_ms"]))   # la primera entrada
                        cands.remove(c)
                        rec, w = c[0], c[1]
                        gv = w["gate"](ci, rec[0]) if with_gate else None
                        if g and not vi and not gv["ok"]:   # ruptura libre: la puerta del lado que rompe
                            continue
                        busy[ci][vi][g] = max(b, rec[2])
                        edge = gv.get("edge_pp") if gv else None
                        out[ci][vi][g].extend((w["open_ms"], w["wi"], cidx, *rec,
                                               (1.0 if gv["ok"] else 0.0) if gv else -1.0,
                                               math.nan if edge is None else edge, float(gv["decided"]) if gv else -1.0))
        if with_gate:   # estas sesiones ya son «anteriores» para las siguientes de cada ventana
            for w in cluster:
                for ci in range(len(combos)):
                    remember(ci, w["wi"], 1, w["recs"][ci][1])
                    remember(ci, w["wi"], -1, w["recs"][ci][-1])
        cluster.clear()

    for o in openings:
        open_ms, wi = o["open_ms"], widx[o["id"]]
        end_ms = open_ms + end_min * MIN
        if cluster and (open_ms >= cl_end or any(w["wi"] == wi for w in cluster)):
            resolve()
        bars = s.slice(open_ms - pre * MIN, end_ms + MIN)
        i0, i_end = apertura.bar_index(bars, open_ms), apertura.bar_index(bars, end_ms)
        if i_end - i0 < COVERAGE * end_min or i0 < 61 or not apertura.pre_coverage_ok(bars, open_ms, p0):
            if with_gate:   # sesión sin datos: cuenta como sesión anterior sin caso (como session_case)
                for ci in range(len(combos)):
                    remember(ci, wi, 1, None)
                    remember(ci, wi, -1, None)
            continue
        prev = apertura.prev_session_opens(o["lead"], open_ms, p0["rvol_sessions"])
        feats: list = [None] * len(bars)
        for i in range(i0, min(i_end, i0 + n_feat)):
            feats[i] = apertura.features(bars, i, open_ms, s.vol_sum, prev, fp)
        ret4 = bars[i_end - 1][C] / bars[i0][O] - 1
        if with_eff:
            before = s.close_before(open_ms - end_min * MIN)
            vb = s.vol_sum(open_ms - 30 * MIN, open_ms)
            eff.append((open_ms, o["id"], ret4 * 100, (bars[i0][O] / before - 1) * 100 if before else None,
                        s.vol_sum(open_ms, open_ms + 30 * MIN) / vb if vb > 0 else None, feats[i0 + 14]["rvol15"]))
        ch = chain.get(f"{o['id']}|{open_ms}")
        if ch is None:   # sin dato de índices: retorno cripto de los 60 min previos
            r60 = bars[i0 - 1][C] / bars[i0 - 61][C] - 1
            ch = 1 if r60 > 0 else -1 if r60 < 0 else 0
        dirs = (0, ch, 1 if ret4 > 0 else -1 if ret4 < 0 else 0)   # libre, encadenamiento, oráculo
        rows.append((open_ms, wi))
        # 1) Primera entrada sin otras posiciones, por combinación y dirección (también son los casos de la puerta).
        recs = [{} for _ in combos]
        for cis in groups.values():
            pg = combos[cis[0]]
            for dk in (1, -1, 0):
                e = find_entry(coin, open_ms, bars, i0, i_end, feats, dk, pg, slip)
                win = None
                for ci in cis:
                    recs[ci][dk], win = record(bars, e, combos[ci], i_end, end_ms, fee, slip, win) if e else (None, None)
        # 2) Se resuelve con las ventanas que se solapan con esta (una sola posición por moneda, por orden de entrada).
        cluster.append({"open_ms": open_ms, "wi": wi, "bars": bars, "i0": i0, "i_end": i_end, "end_ms": end_ms,
                        "feats": feats, "dirs": dirs, "recs": recs, "gate": gate_of(wi)})
        cl_end = max(cl_end, end_ms)
    if cluster:
        resolve()
    span = [s.t[0], s.t[-1], len(s)] if len(s) else [None, None, 0]
    return {"coin": coin, "rows": rows, "eff": eff, "out": out, "lost": lost, "span": span,
            "secs": round(time.time() - t0, 1)}


# ---------------------------------------------------------------- cuenta, estadística y selección

def decode(arr) -> list[tuple]:
    return [tuple(arr[k:k + NC]) for k in range(0, len(arr), NC)]


def merged(results: list[dict], ci: int, vi: int, g: int = 0) -> list[tuple]:
    """Operaciones de todas las monedas de una combinación, variante y puerta, por hora de entrada."""
    rows = []
    for r in results:
        rows += decode(r["out"][ci][vi][g])
    rows.sort(key=lambda x: (x[I_ENTRY], x[I_COIN]))
    return rows


_DAYS: dict[int, str] = {}


def mad_day(ms: float) -> str:
    """Fecha de Madrid (el día de trading del paper) de un instante en ms."""
    h = int(ms // 3600_000)
    d = _DAYS.get(h)
    if d is None:
        d = _DAYS[h] = datetime.fromtimestamp(h * 3600, MAD).date().isoformat()
    return d


def account_sim(rows: list[tuple], p: dict, days: list[str], lev: float = 1.0, take: list | None = None) -> dict:
    """Cuenta paper día a día (día de Madrid) con las operaciones ordenadas por entrada, como en vivo: tamaño por riesgo
    (RISK_PER_TRADE_PCT en el stop) con tope de nominal (paper.MAX_NOTIONAL_PCT), exposición y posiciones máximas,
    pérdida máxima del día y límites diarios del motor (apertura.day_limits, con el riesgo efectivo comprometido).
    lev multiplica el nominal (y con él ganancias, pérdidas y comisiones). take: las operaciones que se toman (las
    de 1:1) sin volver a mirar límites. Devuelve taken (bool por operación), skip (por qué no se tomó: «limite» del
    día, «cuenta» si lo impidió la exposición, las posiciones o la pérdida del día, o None), days [(fecha, % del día,
    entradas, tocó el límite diario, la exposición máxima o la pérdida del día lo impidió)] y equity final."""
    risk = config.RISK_PER_TRADE_PCT
    cap, expo_max = paper.MAX_NOTIONAL_PCT / 100, paper.MAX_EXPOSURE_PCT / 100
    eq = config.PAPER_EQUITY
    taken = [False] * len(rows)
    skip: list[str | None] = [None] * len(rows)
    by_day: dict[str, list[int]] = {}
    for k, r in enumerate(rows):
        by_day.setdefault(mad_day(r[I_ENTRY]), []).append(k)
    out = []
    for day in days:
        start, n, committed, lim, other = eq, 0, 0.0, False, False
        pos: list[list] = []   # [salida, nominal, resultado]
        for k in by_day.get(day, ()):
            r = rows[k]
            keep = []
            for q in pos:   # lo que ya salió antes de esta entrada
                if q[0] <= r[I_ENTRY]:
                    eq += q[2]
                else:
                    keep.append(q)
            pos = keep
            base = min(eq * risk / r[I_STOP], eq * cap)
            if take is None:
                if apertura.day_limits(n, committed, risk, p):
                    lim, skip[k] = True, "limite"
                    continue
                if ((start - eq) / start * 100 >= config.MAX_DAILY_LOSS_PCT or len(pos) >= config.MAX_POSITIONS
                        or sum(q[1] for q in pos) + base > eq * expo_max + 1e-6):
                    other, skip[k] = True, "cuenta"
                    continue
            elif not take[k]:
                continue
            taken[k] = True
            n += 1
            committed += base * r[I_STOP] / eq   # riesgo efectivo en % del capital (como apertura._day_ledger)
            pos.append([r[I_EXIT], base * lev, base * lev * r[I_RET] / 100])
        for q in pos:
            eq += q[2]
        out.append((day, (eq / start - 1) * 100, n, lim, other))
    return {"taken": taken, "skip": skip, "days": out, "equity": eq}


def _q(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    k = (len(xs) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def stats(recs: list[tuple]) -> dict:
    """Por apertura (como la v1). recs: (open_ms, ventana, moneda, % neto de la apertura, operaciones, ganadoras)."""
    n = len(recs)
    if not n:
        return {"openings": 0, "trades": 0}
    rets = sorted(r[3] for r in recs)
    trades = sum(r[4] for r in recs)
    wins = sum(r[5] for r in recs)
    traded = [r[3] for r in recs if r[4] > 0]
    mean = sum(rets) / n
    sd = statistics.pstdev(rets) if n > 1 else 0.0
    cum = peak = dd = 0.0
    for r in sorted(recs, key=lambda r: (r[0], r[1], r[2])):
        cum += r[3]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    r4 = lambda x: round(x, 4)
    return {"openings": n, "with_trades": len(traded), "trades": trades,
            "reentries": sum(max(0, r[4] - 1) for r in recs),
            "win_rate": r4(wins / trades) if trades else None,
            "mean_pct": r4(mean), "median_pct": r4(_q(rets, 0.5)), "p10_pct": r4(_q(rets, 0.1)),
            "p90_pct": r4(_q(rets, 0.9)), "share_ge_3": r4(sum(x >= TARGET_PCT for x in rets) / n),
            "share_ge_3_traded": r4(sum(x >= TARGET_PCT for x in traded) / len(traded)) if traded else None,
            "mean_traded_pct": r4(sum(traded) / len(traded)) if traded else None,
            "mean_trade_pct": r4(sum(rets) / trades) if trades else None,
            "total_pct": r4(sum(rets)), "max_dd_pct": r4(dd),
            "t_stat": r4(mean / (sd / math.sqrt(n))) if sd > 0 else 0.0}


def _small(st: dict) -> dict:
    return {k: st.get(k) for k in ("openings", "trades", "win_rate", "mean_pct", "median_pct", "share_ge_3", "t_stat")}


def trade_stats(rows: list[tuple]) -> dict:
    """Por operación: R (resultado neto / stop), motivos de salida, MFE/MAE y P(MFE ≥ +3 % neto antes del stop)."""
    n = len(rows)
    if not n:
        return {"trades": 0}
    Rs = sorted(r[I_R] for r in rows)
    rets = [r[I_RET] for r in rows]
    mean = sum(Rs) / n
    sd = statistics.pstdev(Rs) if n > 1 else 0.0
    gain, loss = sum(x for x in rets if x > 0), -sum(x for x in rets if x < 0)
    why = {}
    for r in rows:
        w = WHYS[int(r[I_WHY])]
        why[w] = why.get(w, 0) + 1
    g3 = TARGET_PCT + COST_RT_PCT
    r4 = lambda x: round(x, 4)
    avg = lambda k: r4(sum(r[k] for r in rows) / n)
    return {"trades": n, "win_rate": r4(sum(x > 0 for x in rets) / n), "mean_R": r4(mean), "median_R": r4(_q(Rs, 0.5)),
            "p10_R": r4(_q(Rs, 0.1)), "p90_R": r4(_q(Rs, 0.9)), "total_R": round(sum(Rs), 2),
            "mean_ret_pct": r4(sum(rets) / n), "mean_stop_pct": avg(I_STOP),
            "profit_factor": round(gain / loss, 3) if loss else None,
            "t_stat": r4(mean / (sd / math.sqrt(n))) if sd > 0 else 0.0,
            "exit": {w: r4(c / n) for w, c in why.items()}, "partial": avg(I_PART),
            "share_ret_ge_3": r4(sum(x >= TARGET_PCT for x in rets) / n),
            "mfe_ge_3_before_stop": avg(I_HIT3), "mfe_ge_3_window": r4(sum(r[I_MFEW] >= g3 for r in rows) / n),
            "mean_mfe_pct": avg(I_MFE), "mean_mae_pct": avg(I_MAE), "mean_mfe_window_pct": avg(I_MFEW),
            "mean_mae_window_pct": avg(I_MAEW)}


def _tsmall(st: dict) -> dict:
    return {k: st.get(k) for k in ("trades", "win_rate", "mean_R", "median_R", "total_R", "mean_ret_pct", "t_stat",
                                   "exit", "mfe_ge_3_before_stop")}


def day_stats(days: list[tuple]) -> dict:
    """Distribución del % diario de la cuenta (lo que el usuario piensa: «% al día»)."""
    n = len(days)
    if not n:
        return {"days": 0}
    xs = [d[1] for d in days]
    s = sorted(xs)
    eq = peak = 1.0
    dd = 0.0
    for x in xs:
        eq *= 1 + x / 100
        peak = max(peak, eq)
        dd = max(dd, 1 - eq / peak)
    r4 = lambda x: round(x, 4)
    share = lambda f: r4(sum(1 for x in xs if f(x)) / n)
    return {"days": n, "traded_days": sum(1 for d in days if d[2]), "trades": sum(d[2] for d in days),
            "mean_pct": r4(sum(xs) / n), "median_pct": r4(_q(s, 0.5)), "p5_pct": r4(_q(s, 0.05)),
            "p95_pct": r4(_q(s, 0.95)), "worst_pct": r4(s[0]), "best_pct": r4(s[-1]),
            "share_ge_1": share(lambda x: x >= 1), "share_ge_3": share(lambda x: x >= 3),
            "share_ge_5": share(lambda x: x >= 5), "share_le_m1": share(lambda x: x <= -1),
            "share_neg": share(lambda x: x < 0), "limit_days": sum(1 for d in days if d[3]),
            "limit_share": r4(sum(1 for d in days if d[3]) / n), "blocked_days": sum(1 for d in days if d[4]),
            "total_pct": r4((eq - 1) * 100), "max_dd_pct": r4(dd * 100)}


def split_days(days: list[tuple], split_day: str) -> dict:
    return {"is": day_stats([d for d in days if d[0] < split_day]),
            "oos": day_stats([d for d in days if d[0] >= split_day])}


def evaluate(rows: list[tuple], p: dict, days: list[str], split_ms: int) -> dict:
    """Operaciones (ordenadas por entrada) con los límites de la cuenta aplicados, como en vivo, y sus estadísticas."""
    sim = account_sim(rows, p, days)
    taken = [r for r, t in zip(rows, sim["taken"]) if t]
    return {"rows": rows, "sim": sim, "taken": taken,
            "is": trade_stats([r for r in taken if r[I_OPEN] < split_ms]),
            "oos": trade_stats([r for r in taken if r[I_OPEN] >= split_ms])}


def choose(cands: list[tuple[int, dict]], min_trades: int = MIN_TRADES_SELECT) -> int:
    """Índice con mejor R medio por operación en la muestra (con costes y límites); desempate: más operaciones. Si
    ninguna llega al mínimo de operaciones, entre todas."""
    ok = [c for c in cands if c[1].get("trades", 0) >= min_trades] or cands
    return max(ok, key=lambda c: (c[1].get("mean_R", -99), c[1].get("trades", 0)))[0]


def all_days(results: list[dict]) -> list[str]:
    return sorted({mad_day(t) for r in results for t, _ in r["rows"]})


def split_point(times: list[int]) -> tuple[int, str]:
    """Corte dentro/fuera de muestra: la medianoche de Madrid tras el 70 % de las aperturas. Así operaciones (por
    apertura) y cuenta diaria (por día de Madrid) comparten el corte. Devuelve (ms, fecha del primer día fuera)."""
    t70 = times[int(len(times) * (1 - OOS_SHARE))]
    split_ms = paper.next_midnight_ms(t70 - 1)   # t70 si ya es medianoche
    return split_ms, mad_day(split_ms)


def window_blocks(results: list[dict], ci: int, vi: int, g: int, ev: dict, wids: list[str],
                  split_ms: int) -> list[dict]:
    """Fuera de muestra y por ventana, con la configuración ci/vi/g: entradas encontradas (rupturas válidas tras la
    regla de una posición por moneda), tomadas por la cuenta y las que no, por motivo contado: otra ventana ocupaba la
    moneda («busy», se perdió la ruptura), tope de entradas o presupuesto de riesgo del día («day_limit») y
    exposición, posiciones o pérdida del día («account»)."""
    out = {w: {"window": w, "found": 0, "taken": 0, "busy": 0, "day_limit": 0, "account": 0} for w in wids}
    for r, t, why in zip(ev["rows"], ev["sim"]["taken"], ev["sim"]["skip"]):
        if r[I_OPEN] < split_ms:
            continue
        x = out[wids[int(r[I_WID])]]
        x["found"] += 1
        if t:
            x["taken"] += 1
        elif why == "limite":
            x["day_limit"] += 1
        elif why == "cuenta":
            x["account"] += 1
    for res in results:
        a = res["lost"][ci][vi][g]
        for k in range(0, len(a), 2):
            if a[k] >= split_ms:
                out[wids[int(a[k + 1])]]["busy"] += 1
    return list(out.values())


# ---------------------------------------------------------------- efecto apertura (como la v1)

def opening_effect(results: list[dict]) -> list[dict]:
    """¿La apertura mueve el volumen y el precio? (lo que midió el estudio de NTNU, aquí con nuestros datos)."""
    out = []
    by: dict[tuple, list] = {}
    for r in results:
        for e in r["eff"]:
            by.setdefault((e[1], r["coin"]), []).append(e)
    for (w, c), xs in sorted(by.items()):
        vr = sorted(x[4] for x in xs if x[4])
        aft = [x[2] for x in xs]
        bef = [x[3] for x in xs if x[3] is not None]
        rv = sorted(x[5] for x in xs if x[5] is not None)
        out.append({"window": w, "coin": c, "n": len(xs),
                    "vol_30m_after_vs_before_median": round(_q(vr, 0.5), 3) if vr else None,
                    "rvol15_vs_prev_sessions_median": round(_q(rv, 0.5), 3) if rv else None,
                    "ret_4h_after_mean_pct": round(sum(aft) / len(aft), 4),
                    "abs_ret_4h_after_mean_pct": round(sum(abs(x) for x in aft) / len(aft), 4),
                    "abs_ret_4h_before_mean_pct": round(sum(abs(x) for x in bef) / len(bef), 4) if bef else None,
                    "up_share_4h": round(sum(x > 0 for x in aft) / len(aft), 4),
                    # Techo absoluto: dirección perfecta desde la apertura, sin stops, 4 h, con costes de ida y vuelta.
                    "perfect_hold_mean_pct": round(sum(abs(x) - COST_RT_PCT for x in aft) / len(aft), 4),
                    "share_move_ge_3_net": round(sum(abs(x) - COST_RT_PCT >= TARGET_PCT for x in aft) / len(aft), 4)})
    return out


# ---------------------------------------------------------------- informe

def per_opening(results: list[dict], taken: list[tuple], wids: list[str]) -> list[tuple]:
    """Registros por apertura cubierta (como la v1): la operación tomada o 0 si no hubo."""
    got = {(int(r[I_COIN]), int(r[I_OPEN])): r[I_RET] for r in taken}
    recs = []
    for res in results:
        k = config.COINS.index(res["coin"])
        for open_ms, wi in res["rows"]:
            ret = got.get((k, open_ms))
            recs.append((open_ms, wids[wi], res["coin"], ret or 0.0, int(ret is not None), int(bool(ret and ret > 0))))
    return recs


def variant_block(recs: list[tuple], taken: list[tuple], wids: list[str], split_ms: int) -> dict:
    """Claves de la v1 (is, oos, all, by_window, by_coin, by_window_coin) más las de operación (trades_is/oos)."""
    def split(sel_r, sel_t) -> dict:
        xs, ts = [x for x in recs if sel_r(x)], [t for t in taken if sel_t(t)]
        return {"is": stats([x for x in xs if x[0] < split_ms]), "oos": stats([x for x in xs if x[0] >= split_ms]),
                "all": stats(xs), "trades_is": trade_stats([t for t in ts if t[I_OPEN] < split_ms]),
                "trades_oos": trade_stats([t for t in ts if t[I_OPEN] >= split_ms])}

    out = split(lambda x: True, lambda t: True)
    out["by_window"] = [{"window": w, **split(lambda x, w=w: x[1] == w, lambda t, k=k: int(t[I_WID]) == k)}
                        for k, w in enumerate(wids)]
    out["by_coin"] = [{"coin": c, **split(lambda x, c=c: x[2] == c, lambda t, k=k: int(t[I_COIN]) == k)}
                      for k, c in enumerate(config.COINS)]
    out["by_window_coin"] = [{"window": w, "coin": c,
                              **split(lambda x, w=w, c=c: x[1] == w and x[2] == c,
                                      lambda t, k=k, m=m: int(t[I_WID]) == k and int(t[I_COIN]) == m)}
                             for k, w in enumerate(wids) for m, c in enumerate(config.COINS)]
    return out


def gate_split(rows: list[tuple], split_ms: int, p: dict) -> dict:
    """Operaciones SIN puerta separadas por lo que habría dicho la puerta en avance (¿predice algo?) y por qué no se
    cumplió: pocos casos decididos, sin ventaja de probabilidad o R medio ≤ 0 de las rupturas pasadas."""
    out = {}
    for part, sel in (("is", lambda r: r[I_OPEN] < split_ms), ("oos", lambda r: r[I_OPEN] >= split_ms)):
        xs = [r for r in rows if sel(r) and r[I_GOK] >= 0]
        n = len(xs) or 1
        few = [r for r in xs if not r[I_GOK] and r[I_GDEC] < p["gate_min_n"]]
        flat = [r for r in xs if not r[I_GOK] and r[I_GDEC] >= p["gate_min_n"]
                and not (r[I_GEDGE] >= p["gate_pp"])]   # NaN cuenta como sin ventaja
        out[part] = {"pass": _tsmall(trade_stats([r for r in xs if r[I_GOK] == 1])),
                     "fail": _tsmall(trade_stats([r for r in xs if r[I_GOK] == 0])),
                     "reasons": {"ok": round(sum(1 for r in xs if r[I_GOK] == 1) / n, 4),
                                 "pocos_casos": round(len(few) / n, 4), "sin_ventaja": round(len(flat) / n, 4),
                                 "r_medio_no_positivo": round((sum(1 for r in xs if not r[I_GOK]) - len(few) - len(flat))
                                                              / n, 4)},
                     "mean_decided": round(sum(r[I_GDEC] for r in xs) / n, 2)}
    return out


def gate_compare(off: dict, on: dict, split_ms: int, split_day: str, p: dict) -> dict:
    """Con y sin puerta, dentro y fuera de muestra (operaciones y cuenta diaria)."""
    return {"is": {"sin": off["is"], "con": on["is"]}, "oos": {"sin": off["oos"], "con": on["oos"]},
            "daily_oos": {"sin": split_days(off["sim"]["days"], split_day)["oos"],
                          "con": split_days(on["sim"]["days"], split_day)["oos"]},
            "by_prediction": gate_split(off["taken"], split_ms, p),
            "params": {k: p[k] for k in ("gate_pp", "gate_min_n", "gate_sessions")}}


def trade_row(r: tuple, wids: list[str], split_ms: int) -> dict:
    nan = lambda x: None if x is None or (isinstance(x, float) and math.isnan(x)) else round(x, 4)
    return {"part": "is" if r[I_OPEN] < split_ms else "oos", "date": mad_day(r[I_OPEN]), "window": wids[int(r[I_WID])],
            "coin": config.COINS[int(r[I_COIN])], "side": "long" if r[I_DIR] > 0 else "short",
            "open": int(r[I_OPEN]), "entry_t": int(r[I_ENTRY]), "exit_t": int(r[I_EXIT]),
            "stop_pct": round(r[I_STOP], 3), "rr": round(r[I_RR], 3) or None, "R": round(r[I_R], 3),
            "ret_pct": round(r[I_RET], 4), "why": WHY_TEXT[WHYS[int(r[I_WHY])]], "partial": bool(r[I_PART]),
            "mfe_pct": round(r[I_MFE], 3), "mae_pct": round(r[I_MAE], 3), "mfe_window_pct": round(r[I_MFEW], 3),
            "mae_window_pct": round(r[I_MAEW], 3), "mfe_ge_3_before_stop": bool(r[I_HIT3]),
            "gate_ok": None if r[I_GOK] < 0 else bool(r[I_GOK]), "gate_edge_pp": nan(r[I_GEDGE])}


def trades_sample(taken: list[tuple], wids: list[str], split_ms: int, n_oos: int = 120, n_is: int = 30) -> list[dict]:
    """Muestra: las n_oos operaciones más recientes (fuera de muestra) y n_is repartidas por la muestra."""
    ins = [r for r in taken if r[I_OPEN] < split_ms]
    oos = [r for r in taken if r[I_OPEN] >= split_ms]
    step = max(1, len(ins) // n_is) if ins else 1
    pick = ins[::step][:n_is] + oos[-n_oos:]
    return [trade_row(r, wids, split_ms) for r in pick]


def _clean(x):
    """JSON válido para la web: sin NaN ni infinitos."""
    if isinstance(x, float):
        return None if math.isnan(x) or math.isinf(x) else x
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    return x


def best_list(variants: dict, top: int = 8) -> list[dict]:
    """Mejores ventana×moneda de las variantes operables, ordenadas por la muestra (no por el resultado fuera)."""
    cand = []
    for v in ("libre", "encadenamiento"):
        for x in variants[v]["by_window_coin"]:
            ins, oos = x["trades_is"], x["trades_oos"]
            if ins.get("trades", 0) < 30:
                continue
            cand.append({"variant": v, "window": x["window"], "coin": x["coin"], "is": _tsmall(ins), "oos": _tsmall(oos),
                         "oos_holds": bool(oos.get("trades") and oos["mean_R"] > 0)})
    return sorted(cand, key=lambda x: -x["is"]["mean_R"])[:top]


def _r(st: dict, k: str = "mean_R", d: int = 3) -> str:
    v = st.get(k)
    return "–" if v is None else _sg(v, d)


def _edge_word(st: dict) -> str:
    """Calificación honesta de un R medio por operación fuera de muestra."""
    if not st.get("trades"):
        return "sin operaciones"
    if st["mean_R"] <= 0:
        return "pierde con costes: no hay ventaja"
    if st["t_stat"] < 2:
        return "positivo pero indistinguible del azar (t < 2): no es una ventaja demostrada"
    return "positivo y con t ≥ 2 en esta muestra (una sola muestra fuera; no es una promesa)"


def verdict(res: dict) -> list[str]:
    V, out = res["variants"], []
    for v in (REC_VARIANT, "libre"):   # las dos operables, sin escoger la mejor mirando fuera de muestra
        b, bo = V[v]["trades_oos"], V[v]["oos"]
        if not b.get("trades"):
            out.append(f"{VARIANTS[v]['name']}: sin operaciones fuera de muestra.")
            continue
        out.append(f"{VARIANTS[v]['name']}{' (la que carga el motor)' if v == REC_VARIANT else ''}, fuera de muestra, con "
                   f"«{V[v]['chosen_text']}» (elegido con el 70 % inicial): R medio {_r(b)} por operación con costes "
                   f"({b['trades']} operaciones, {_pc(b['win_rate'])} ganadoras, {_sg(b['mean_ret_pct'], 3)} % por "
                   f"operación, t {_num(b['t_stat'])}): {_edge_word(b)}. Por apertura: media {_sg(bo['mean_pct'], 3)} % y "
                   f"solo el {_pc(bo['share_ge_3'], 1)} de las aperturas llega a +3 %.")
    b = V[REC_VARIANT]["trades_oos"]
    if b.get("trades"):
        out.append(f"¿+3 % por operación? Fuera de muestra ({VARIANTS[REC_VARIANT]['name']}), el "
                   f"{_pc(b['share_ret_ge_3'], 1)} de las operaciones cierra con ≥ +3 % neto; el precio llegó a +3 % neto "
                   f"antes de tocar el stop inicial en el {_pc(b['mfe_ge_3_before_stop'], 1)} y en algún momento de la "
                   f"ventana en el {_pc(b['mfe_ge_3_window'], 1)}.")
    prev = ((res.get("v1_previous") or {}).get("oos") or {}).get(REC_VARIANT) or {}
    bo = V[REC_VARIANT]["oos"]
    if prev.get("mean_pct") is not None and bo.get("openings") and b.get("trades"):
        out.append(f"Comparación con la v1 ({VARIANTS[REC_VARIANT]['name']}, break-even a +1 R y stop dinámico desde la "
                   f"entrada, archivo anterior): {_sg(prev['mean_pct'], 3)} % por apertura y "
                   f"{_sg(prev.get('mean_trade_pct') or 0, 3)} % por operación fuera de muestra; la v2 da "
                   f"{_sg(bo['mean_pct'], 3)} % por apertura y {_sg(b['mean_ret_pct'], 3)} % por operación (opera menos: "
                   "una entrada por moneda y ventana y como mucho 3 al día).")
    rec = res["daily"].get(REC_VARIANT) or {}
    d1, d2 = (rec.get("lev1") or {}).get("oos") or {}, (rec.get("lev2") or {}).get("oos") or {}
    if d1.get("days"):
        out.append(f"Cuenta de 10.000 USDC ({VARIANTS[REC_VARIANT]['name']}, la que carga el motor; 0,5 % de riesgo, "
                   f"tope de nominal 40 %, sin apalancamiento), fuera de muestra, {d1['days']} días: mediana "
                   f"{_sg(d1['median_pct'], 3)} % al día, media {_sg(d1['mean_pct'], 3)} %, p5 {_sg(d1['p5_pct'])} %, "
                   f"p95 {_sg(d1['p95_pct'])} %, peor día {_sg(d1['worst_pct'])} %. Días ≥ +1 %: {_pc(d1['share_ge_1'], 1)}; "
                   f"≥ +3 %: {_pc(d1['share_ge_3'], 1)}; ≥ +5 %: {_pc(d1['share_ge_5'], 1)}. Total compuesto "
                   f"{_sg(d1['total_pct'])} % y caída máxima {_num(d1['max_dd_pct'])} %. Días con operaciones: "
                   f"{d1.get('traded_days', 0)}; días que tocan el límite diario: {d1.get('limit_days', 0)}.")
    if d2.get("days"):
        out.append(f"Con apalancamiento 2:1 (máximo para minoristas en España en derivados de cripto) las mismas "
                   f"operaciones dan media {_sg(d2['mean_pct'], 3)} % al día, peor día {_sg(d2['worst_pct'])} %, total "
                   f"{_sg(d2['total_pct'])} % y caída máxima {_num(d2['max_dd_pct'])} %: el apalancamiento amplifica las "
                   "pérdidas igual que las ganancias y no convierte una media negativa en positiva.")
    gt = res["gate"].get(REC_VARIANT) or {}
    empty = True
    for g, what in ((gt, "con la salida elegida"), (gt.get("reference") or {}, "con el preset V2 del motor tal cual (" +
                                                   ((gt.get("reference") or {}).get("text") or "") + ")")):
        opts = sorted((g.get("options") or {}).items(), key=lambda x: int(x[0]))
        if not opts:
            continue
        off, doff = opts[0][1]["oos"]["sin"], opts[0][1]["daily_oos"]["sin"]
        parts = []
        for gs, o in opts:
            on, rs, don = o["oos"]["con"], o["by_prediction"]["oos"]["reasons"], o["daily_oos"]["con"]
            if not on.get("trades"):
                parts.append(f"con {gs} sesiones de historia no deja entrar nunca (pocos casos decididos en el "
                             f"{_pc(rs['pocos_casos'], 1)} de las rupturas, sin ventaja de probabilidad "
                             f"{_pc(rs['sin_ventaja'], 1)}, R medio pasado ≤ 0 {_pc(rs['r_medio_no_positivo'], 1)})")
            else:
                empty = False
                parts.append(f"con {gs} sesiones, R medio {_r(on)} en {on['trades']} operaciones "
                             f"({_sg(don.get('mean_pct', 0), 3)} % al día; dejó pasar el {_pc(rs['ok'], 1)} de las "
                             f"rupturas): {_edge_word(on)}")
        out.append(f"Puerta de probabilidades en avance ({VARIANTS[REC_VARIANT]['name']}, {what}), fuera de muestra: sin "
                   f"puerta R medio {_r(off)} en {off.get('trades', 0)} operaciones ({_sg(doff.get('mean_pct', 0), 3)} % "
                   f"al día); " + "; ".join(parts) + ".")
    if gt:
        out.append(f"La puerta {'se activa (' + str(gt.get('sessions')) + ' sesiones)' if gt.get('chosen') else 'no se activa'}"
                   f" en la recomendación: la regla fijada de antemano exige que en la muestra suba el R medio y deje al "
                   f"menos {MIN_TRADES_GATE} operaciones."
                   + (" Una puerta que no deja entrar nunca solo evita operar una regla que pierde con costes; no crea "
                      "ventaja." if empty else ""))
    blk = {x["window"]: x for x in V[REC_VARIANT].get("windows_oos") or []}
    tot, ny = sum(x["taken"] for x in blk.values()), blk.get("nuevayork")
    if ny and tot and ny["taken"] / tot < NY_LOW_SHARE:   # solo si de verdad opera poco, y con los motivos contados
        cand = ny["found"] + ny["busy"]
        out.append(f"Nueva York opera poco con la configuración global: {ny['taken']} de {tot} operaciones fuera de "
                   f"muestra ({_pc(ny['taken'] / tot, 1)}). De sus {cand} rupturas con entrada fuera de muestra se "
                   f"tomaron {ny['taken']}; {ny['busy']} se perdieron porque otra ventana ocupaba la moneda, "
                   f"{ny['day_limit']} por el tope de entradas o el presupuesto de riesgo del día (día de Madrid) y "
                   f"{ny['account']} por exposición, posiciones o pérdida del día. Por eso Nueva York se mide también por "
                   "separado.")
    o = V["oraculo"]["trades_oos"]
    if o.get("trades"):
        out.append(f"Oráculo (TECHO INALCANZABLE, dirección perfecta de las 4 h) con su mejor salida de la muestra "
                   f"(«{V['oraculo']['chosen_text']}»): R medio {_r(o)} fuera de muestra y {_pc(o['share_ret_ge_3'], 1)} "
                   "de operaciones ≥ +3 %. Es el límite de lo que podría aportar una señal perfecta; no es operable.")
    for v in (REC_VARIANT, "oraculo"):
        fx = res["exits"].get("focus", {}).get(v) or {}
        if not fx.get("oos"):
            continue
        dd = (fx.get("daily") or {}).get("oos") or {}
        out.append(f"Solo Nueva York en SOL/ETH/XRP ({VARIANTS[v]['name']}, salida elegida solo con esa parte de la "
                   f"muestra: «{fx['text']}»): R medio {_r(fx['oos'])} fuera de muestra en {fx['oos'].get('trades', 0)} "
                   f"operaciones (t {_num(fx['oos'].get('t_stat') or 0)}); {_edge_word(fx['oos'])}"
                   + (f". En cuenta: {_sg(dd['mean_pct'], 3)} % de media al día, días ≥ +1 % "
                      f"{_pc(dd['share_ge_1'], 1)}, peor día {_sg(dd['worst_pct'])} %." if dd.get("days") else "."))
    eff = res.get("opening_effect") or []
    if eff:
        vr = [e["vol_30m_after_vs_before_median"] for e in eff if e["vol_30m_after_vs_before_median"]]
        drift = [e["ret_4h_after_mean_pct"] for e in eff]
        up = [e["up_share_4h"] for e in eff]
        ph = [e["perfect_hold_mean_pct"] for e in eff]
        g3 = [e["share_move_ge_3_net"] for e in eff]
        out.append(f"Techo absoluto (dirección perfecta desde la apertura, sin stops, 4 h, con costes): media "
                   f"{_sg(min(ph))} a {_sg(max(ph))} % por apertura según ventana y moneda; la moneda se mueve ≥ 3 % neto en "
                   f"4 h solo en el {_pc(min(g3), 1)}–{_pc(max(g3), 1)} de las aperturas.")
        out.append(f"Efecto apertura: el volumen de los 30 min tras la apertura es {_num(min(vr))}–{_num(max(vr))} veces "
                   f"el de los 30 min anteriores, pero el retorno medio de las 4 h siguientes va de {_sg(min(drift))} a "
                   f"{_sg(max(drift))} % y sube en el {_pc(min(up))}–{_pc(max(up))} de los días: más volumen, sin "
                   "dirección propia.")
    return out


def summary_text(res: dict) -> list[str]:
    L = []
    per = res["period"]
    L.append(f"Backtest v2 del motor de aperturas · {per['from']} a {per['to']} · {res['openings']['total']} aperturas "
             f"× {len(config.COINS)} monedas · fuera de muestra desde {per['split']}")
    L.append(f"Costes: {_num(config.FEE_PCT)} % de comisión y {_num(SLIP_BPS, 0)} pb de deslizamiento por lado. "
             "R = resultado neto / stop inicial.")
    for v, x in res["variants"].items():
        L.append("")
        L.append(f"[{x['name']}] elegido con el 70 % inicial: {x['chosen_text']}")
        for part, label in (("trades_is", "Muestra (70 %)"), ("trades_oos", "Fuera de muestra (30 %)")):
            s = x[part]
            if not s.get("trades"):
                L.append(f"  {label}: sin operaciones")
                continue
            ex = ", ".join(f"{WHY_TEXT[w]} {_pc(c)}" for w, c in sorted(s["exit"].items(), key=lambda z: -z[1]))
            L.append(f"  {label}: {s['trades']} operaciones, {_pc(s['win_rate'])} ganadoras, R medio {_r(s)} "
                     f"(mediana {_r(s, 'median_R')}), {_sg(s['mean_ret_pct'], 3)} % por operación, t {_num(s['t_stat'])} · "
                     f"salidas: {ex} · MFE ≥ +3 % antes del stop {_pc(s['mfe_ge_3_before_stop'], 1)}")
        top = sorted(x["by_window"], key=lambda w: -(w["trades_oos"].get("mean_R") if w["trades_oos"].get("trades")
                                                      else -99))
        L.append("  Por ventana (fuera de muestra): " + "; ".join(
            f"{w['window']} R {_r(w['trades_oos'])} ({w['trades_oos'].get('trades', 0)} ops)" for w in top))
    for v, d in res["daily"].items():
        for lev in ("lev1", "lev2"):
            s = d[lev]["oos"]
            if s.get("days"):
                L.append(f"Cuenta {VARIANTS[v]['name']} {lev[-1]}:1, fuera de muestra: mediana {_sg(s['median_pct'], 3)} % "
                         f"al día, media {_sg(s['mean_pct'], 3)} %, p5 {_sg(s['p5_pct'])} %, p95 {_sg(s['p95_pct'])} %, "
                         f"peor {_sg(s['worst_pct'])} %, días ≥ +1 % {_pc(s['share_ge_1'], 1)}, ≥ +3 % "
                         f"{_pc(s['share_ge_3'], 1)}, ≥ +5 % {_pc(s['share_ge_5'], 1)}, total {_sg(s['total_pct'])} %")
    L.append("")
    L.extend(res["verdict"])
    return L


# ---------------------------------------------------------------- principal

def _pool(jobs: list[tuple], procs: int) -> list[dict]:
    if procs > 1 and len(jobs) > 1:
        with ProcessPoolExecutor(max_workers=min(procs, len(jobs))) as ex:
            return list(ex.map(run_coin, jobs))
    return [run_coin(j) for j in jobs]


def previous_v1() -> dict | None:
    """Cifras de la v1 (aperturas.json anterior o su copia aperturas-v1.json) para compararlas en el veredicto."""
    old = None
    for name in ("aperturas.json", "aperturas-v1.json"):
        try:
            x = json.loads((config.BACKTEST_DIR / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not x.get("version") and x.get("variants"):
            old = x
            break
    if old is None:
        return None
    keys = ("openings", "trades", "win_rate", "mean_pct", "mean_trade_pct", "share_ge_3")
    return {"period": old.get("period"), "chosen": (old.get("params") or {}).get("chosen"),
            "oos": {v: {k: (x.get("oos") or {}).get(k) for k in keys} for v, x in old["variants"].items()}}


def run_backtest(months: int, offline: bool, procs: int) -> dict:
    today = datetime.now(timezone.utc).date()
    mlist, current = month_list(today, months)
    data = asyncio.run(ensure_data(mlist, current, offline))
    for e in data["errors"]:
        log.warning("Datos: %s", e)
    idx = index_data()
    openings = build_openings(date.fromisoformat(mlist[0] + "-01"), today - timedelta(days=1), idx)
    chain = {}
    for o in openings:
        c = chain_signal(o, idx)
        if c is not None:
            chain[f"{o['id']}|{o['open_ms']}"] = c[0]
    wids = sorted({o["id"] for o in openings}, key=lambda w: min(o["open_ms"] % 86400_000 for o in openings
                                                               if o["id"] == w))
    times = sorted(o["open_ms"] for o in openings)
    split_ms, split_day = split_point(times)
    fee, slip = config.FEE_PCT / 100, SLIP_BPS / 1e4
    coins = [c for c in config.COINS if data["files"][c]]

    def jobs(combos: list[dict], with_gate: bool, with_eff: bool) -> list[tuple]:
        return [(c, [str(p) for p in data["files"][c]], openings, chain, [x["params"] for x in combos], wids, fee, slip,
                 with_gate, with_eff) for c in coins]

    # Etapa 1: rejilla de salidas con las entradas de la v1, sin puerta.
    c1 = exit_combos()
    log.info("Etapa 1: %d aperturas × %d monedas × %d salidas × %d variantes…", len(openings), len(coins), len(c1),
             len(VARIANTS))
    t0 = time.time()
    res1 = _pool(jobs(c1, False, True), procs)
    log.info("Etapa 1 hecha en %.0f s (%s)", time.time() - t0, ", ".join(f"{r['coin']} {r['secs']} s" for r in res1))
    days = all_days(res1)
    eff = opening_effect(res1)
    span = {r["coin"]: r["span"] for r in res1}
    fwi = {wids.index(w) for w in FOCUS["windows"] if w in wids}
    fco = {config.COINS.index(c) for c in FOCUS["coins"]}
    in_focus = lambda r: int(r[I_WID]) in fwi and int(r[I_COIN]) in fco
    exits: dict = {"grid": {}, "chosen": {}, "focus": {}}
    best1 = {}
    for vi, v in enumerate(VKEYS):
        grid, fgrid = [], []
        for ci, c in enumerate(c1):
            rows = merged(res1, ci, vi)
            ev = evaluate(rows, c["params"], days, split_ms)
            grid.append({"label": c["label"], "text": combo_text(c["label"]), "is": _tsmall(ev["is"]),
                         "oos": _tsmall(ev["oos"])})
            fv = evaluate([r for r in rows if in_focus(r)], c["params"], days, split_ms)
            fgrid.append((ci, fv["is"], fv["oos"]))
        best1[v] = choose([(ci, g["is"]) for ci, g in enumerate(grid)])
        fb = choose([(ci, x[1]) for ci, x in enumerate(fgrid)], MIN_TRADES_SELECT // 2)
        frows = [r for r in merged(res1, fb, vi) if in_focus(r)]   # cuenta diaria si solo operara el foco
        exits["grid"][v] = grid
        exits["chosen"][v] = {"index": best1[v], "label": c1[best1[v]]["label"], "text": grid[best1[v]]["text"]}
        exits["focus"][v] = {"windows": FOCUS["windows"], "coins": FOCUS["coins"], "index": fb,
                             "label": c1[fb]["label"], "text": combo_text(c1[fb]["label"]),
                             "is": fgrid[fb][1], "oos": fgrid[fb][2],
                             "daily": split_days(account_sim(frows, c1[fb]["params"], days)["days"], split_day),
                             "chosen_overall_is": fgrid[best1[v]][1], "chosen_overall_oos": fgrid[best1[v]][2]}
    del res1

    # Etapa 2: rango y rvol con la mejor salida de cada variante, sin y con puerta (60 y 250 sesiones de historia),
    # y el preset V2 del motor tal cual como referencia de la puerta.
    c2, keys = [], {}

    def add(lab: dict) -> int:
        key = json.dumps(lab, sort_keys=True)
        if key not in keys:
            keys[key] = len(c2)
            c2.append(make_combo(**lab))
        return keys[key]

    ref = {gs: add({**REF_V2, "gate_sessions": gs}) for gs in GATE_SESSIONS}
    owner = {v: [{gs: add({**c1[best1[v]]["label"], "range_min": rm, "rvol_min": rv, "gate_sessions": gs})
                  for gs in GATE_SESSIONS} for rm, rv in itertools.product(*ENTRY_GRID.values())] for v in VKEYS}
    log.info("Etapa 2: %d combinaciones (rango, rvol, historia de la puerta y el preset V2), con y sin puerta…", len(c2))
    t0 = time.time()
    res2 = _pool(jobs(c2, True, False), procs)
    log.info("Etapa 2 hecha en %.0f s", time.time() - t0)
    variants, daily, gate, samples = {}, {}, {}, {}
    gs0 = GATE_SESSIONS[0]
    for vi, v in enumerate(VKEYS):
        # Sin puerta las operaciones no dependen de la historia de la puerta: se elige con la de 60.
        evs = {o[gs0]: evaluate(merged(res2, o[gs0], vi), c2[o[gs0]]["params"], days, split_ms) for o in owner[v]}
        ci = choose([(k, e["is"]) for k, e in evs.items()])
        twins = next(o for o in owner[v] if o[gs0] == ci)
        off = evs[ci]
        ons = {gs: evaluate(merged(res2, k, vi, 1), c2[k]["params"], days, split_ms) for gs, k in twins.items()}
        # Puerta: solo si en la muestra sube el R medio y deja al menos MIN_TRADES_GATE operaciones (regla previa).
        ok = [gs for gs, e in ons.items() if e["is"].get("trades", 0) >= MIN_TRADES_GATE
              and e["is"]["mean_R"] > off["is"].get("mean_R", -99)]
        use_gs = max(ok, key=lambda gs: ons[gs]["is"]["mean_R"]) if ok else None
        use_gate = use_gs is not None
        fin = ons[use_gs] if use_gate else off
        fin_ci = twins[use_gs] if use_gate else ci
        p = c2[fin_ci]["params"]
        chosen = {k: p[k] for k in REC_KEYS}
        chosen["gate"] = use_gate
        recs = per_opening(res2, fin["taken"], wids)
        variants[v] = {**VARIANTS[v], "chosen": chosen, "chosen_label": c2[ci]["label"],
                       "chosen_text": combo_text(c2[ci]["label"]) + (f" · puerta con {use_gs} sesiones" if use_gate
                                                                     else " · sin puerta"),
                       "grid": [{"params": c2[k]["label"], "text": combo_text(c2[k]["label"]), "is": _tsmall(e["is"]),
                                 "oos": _tsmall(e["oos"])} for k, e in evs.items()],
                       "windows_oos": window_blocks(res2, fin_ci, vi, int(use_gate), fin, wids, split_ms),
                       **variant_block(recs, fin["taken"], wids, split_ms)}
        lev2 = account_sim(fin["rows"], p, days, lev=2.0, take=fin["sim"]["taken"])
        d2 = [(a[0], a[1], a[2], b[3], b[4]) for a, b in zip(lev2["days"], fin["sim"]["days"])]   # mismos límites
        daily[v] = {"label": VARIANTS[v]["name"], "gate": use_gate,
                    "lev1": split_days(fin["sim"]["days"], split_day), "lev2": split_days(d2, split_day)}
        # Cada historia con sus propias columnas de puerta (las operaciones sin puerta son las mismas).
        ev = lambda k, g=0: evaluate(merged(res2, k, vi, g), c2[k]["params"], days, split_ms)
        gate[v] = {"chosen": use_gate, "sessions": use_gs,
                   "options": {str(gs): gate_compare(evs[k] if k in evs else ev(k), ons[gs], split_ms, split_day,
                                                     c2[k]["params"]) for gs, k in twins.items()},
                   "reference": {"text": combo_text(c2[ref[gs0]]["label"]), "label": c2[ref[gs0]]["label"],
                                 "options": {str(gs): gate_compare(ev(k), ev(k, 1), split_ms, split_day, c2[k]["params"])
                                             for gs, k in ref.items()}}}
        samples[v] = trades_sample(fin["taken"], wids, split_ms)
    del res2
    day = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).date().isoformat() if ms else None
    wins = {}
    for o in openings:
        wins.setdefault(o["id"], {"name": o["name"], "markets": o["markets"], "n": 0})["n"] += 1
    res = {"generated": now_ms(), "version": VERSION,
           "period": {"from": day(times[0]), "to": day(times[-1]), "split": split_day, "months": months,
                      "offline": offline},
           "params": {"base": {k: BASE[k] for k in REC_KEYS}, "exit_grid": EXIT_GRID, "targets": TARGETS,
                      "entry_grid": ENTRY_GRID, "fee_pct": config.FEE_PCT, "slip_bps": SLIP_BPS, "oos_share": OOS_SHARE,
                      "min_trades_select": MIN_TRADES_SELECT, "min_trades_gate": MIN_TRADES_GATE,
                      "account": {"equity": config.PAPER_EQUITY, "risk_pct": config.RISK_PER_TRADE_PCT,
                                  "max_notional_pct": paper.MAX_NOTIONAL_PCT, "max_exposure_pct": paper.MAX_EXPOSURE_PCT,
                                  "max_positions": config.MAX_POSITIONS, "max_daily_loss_pct": config.MAX_DAILY_LOSS_PCT},
                      "chosen": {v: x["chosen"] for v, x in variants.items()}},
           "data": {c: [day(sp[0]), day(sp[1]), sp[2]] for c, sp in span.items()},
           "data_errors": data["errors"],
           "openings": {"total": len(openings), "by_window": wins},
           "variants": variants, "opening_effect": eff, "exits": exits, "gate": gate, "daily": daily,
           "trades_sample": {"variant": REC_VARIANT, "trades": samples[REC_VARIANT],
                             "note": "Muestra: las 120 operaciones más recientes (fuera de muestra) y 30 repartidas por "
                                     "la muestra, con la configuración que carga el motor."},
           "recommended": variants[REC_VARIANT]["chosen"],
           "v1_previous": previous_v1()}
    res["best"] = best_list(variants)
    res["verdict"] = verdict(res)
    res["notes"] = [
        "Precios de Binance al contado en USDT como aproximación a OKX EU en USDC.",
        f"Costes: {_num(config.FEE_PCT)} % de comisión por lado y {_num(SLIP_BPS, 0)} pb de deslizamiento por lado.",
        "Entrada en la apertura de la vela siguiente a la que confirma la ruptura (sin mirar el futuro); el R:R mínimo "
        "y el recorrido ≥ 3 × coste se miden desde esa entrada, como en vivo.",
        "Mismas funciones que el motor y el paper: CoinRun, entry_plan, simulate_plan, gate_verdict y day_limits. "
        "Dentro de cada vela, supuesto pesimista: primero el stop, después parcial y objetivo, después el stop nuevo.",
        "Una entrada por moneda y ventana (sin reentradas) y una sola posición por moneda: con ventanas solapadas "
        "(Tokio-Seúl y China, São Paulo y Nueva York) entra la primera ruptura, sea de la ventana que sea (por orden de "
        "entrada, como en vivo), y la otra no entra hasta su salida.",
        f"Corte dentro/fuera de muestra en la medianoche de Madrid tras el 70 % de las aperturas ({res['period']['split']}"
        "): las operaciones y la cuenta diaria usan el mismo corte.",
        "Bandera MOVING: no se entra si la moneda ya se movió ≥ 3 % en las 4 h previas a la apertura.",
        "Cuenta: 10.000 USDC, riesgo 0,5 % en el stop con tope de nominal del 40 %, exposición ≤ 100 %, 4 posiciones, "
        "pérdida del día ≤ 3 %, como mucho 3 entradas al día y 1,5 % de riesgo comprometido al día (día de Madrid). El "
        "tamaño se calcula con el capital realizado. Aproximaciones pequeñas: si un límite quita una operación, la "
        "ventana solapada de esa moneda no recupera la ruptura que había bloqueado, y si la exposición o la pérdida del "
        "día impiden una entrada no se busca otra ruptura en esa ventana (en vivo se reintentaría a los 5 min).",
        "Apalancamiento 2:1: las mismas operaciones con el doble de nominal (y de riesgo en el stop). Sin costes de "
        "financiación de derivados ni liquidaciones (con stops ≤ 3 % no se llega). Amplifica pérdidas igual que ganancias.",
        "Puerta de probabilidades en avance: para cada apertura, las rupturas de las 60 (preset V2) o 250 sesiones "
        "anteriores de la misma ventana y moneda con la misma regla; P(objetivo antes que stop) ≥ punto muerto + 5 "
        "puntos, R medio > 0 y al menos 20 casos decididos (que tocaron objetivo o stop). Los primeros meses no tienen "
        "historia suficiente y no operan con puerta.",
        "Selección SOLO con el 70 % inicial de las aperturas: mejor R medio por operación (con costes y con los límites "
        f"de la cuenta), con al menos {MIN_TRADES_SELECT} operaciones; la puerta se activa si en la muestra sube el R "
        f"medio y deja al menos {MIN_TRADES_GATE} operaciones. El 30 % final no se usó para elegir nada.",
        "Fuera de muestra no del todo limpio: el diseño de la v2 (stops anchos, sin break-even a +1 R, objetivo fijo) se "
        "inspiró en el resultado de la v1 sobre estos mismos datos, y algunas reglas base se fijaron mirándolos.",
        "El t supone operaciones independientes; las cuatro monedas se mueven juntas, así que el t real es menor.",
        "El oráculo es un TECHO INALCANZABLE: usa la dirección real de las 4 h siguientes, que nadie conoce de antemano.",
        "El encadenamiento usa el cierre de la bolsa que cerró justo antes (5 min de margen); sin dato del índice, el "
        "retorno cripto de los 60 min previos.",
        "La señal en vivo (noticias, plan del estratega, predicciones) no tiene historia suficiente para backtest: "
        "encadenamiento y oráculo acotan por abajo y por arriba lo que puede aportar.",
    ]
    return _clean(res)


def out_name(months: int, offline: bool, salida: str | None = None) -> str:
    """Archivo de resultados en BACKTEST_DIR. aperturas.json (lo que carga el motor y lee la web) solo con la pasada
    completa: al menos REC_MIN_MONTHS meses y con red. Una pasada corta o sin red va a su propio archivo y no pisa la
    recomendación. --salida lo fija a mano (solo el nombre: siempre dentro de BACKTEST_DIR)."""
    if salida:
        return Path(salida).name
    if months >= apertura.REC_MIN_MONTHS and not offline:
        return "aperturas.json"
    return f"aperturas-{months}m{'-offline' if offline else ''}.json"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m bot.backtest_aperturas", description="Backtest v2 del motor de aperturas")
    ap.add_argument("--meses", type=int, default=24, help="meses completos de historia (por defecto 24)")
    ap.add_argument("--offline", action="store_true", help="usar solo la caché, sin red")
    ap.add_argument("--procesos", type=int, default=min(4, os.cpu_count() or 1), help="procesos en paralelo")
    ap.add_argument("--salida", default=None,
                    help="nombre del archivo en BACKTEST_DIR (por defecto aperturas.json solo con 24 meses o más y con "
                         "red; si no, aperturas-<meses>m[-offline].json)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    res = run_backtest(a.meses, a.offline, a.procesos)
    config.BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    path = config.BACKTEST_DIR / out_name(a.meses, a.offline, a.salida)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(res, ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    os.replace(tmp, path)
    out = "\n".join(summary_text(res))
    try:
        print(out)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((out + "\n").encode("utf-8"))
    print(f"\nGuardado en {path}")
    if path.name == "aperturas.json" and (a.meses < apertura.REC_MIN_MONTHS):
        print(f"Aviso: con {a.meses} meses el motor no carga esta recomendación (necesita {apertura.REC_MIN_MONTHS}).")


if __name__ == "__main__":
    main()
