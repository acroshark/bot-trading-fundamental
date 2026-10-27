"""Datos históricos para el backtest: índices diarios (Yahoo), velas de cripto (Binance) y ratio largo/corto horario
(futuros de Binance). Todo se cachea en JSON en config.HIST_DIR y se refresca si tiene más de 20 h."""
import asyncio
import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

from . import config, regions

log = logging.getLogger("history")

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
BINANCE = "https://data-api.binance.vision/api/v3/klines"
LS_URL = "https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0.0.0 Safari/537.36")
PAIRS = {c: f"{c}USDT" for c in config.COINS}
MAX_AGE_S = 20 * 3600
HOURLY_DAYS = 730
PAGE_PAUSE_S = 0.2       # pausa entre páginas de Binance para no rozar el límite de peso
H_MS = 3600_000
STEP_MS = {"1h": H_MS, "1d": 24 * H_MS}


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------- Rutas y caché ----------
def index_path(key: str) -> Path:
    return config.HIST_DIR / "indices" / f"{key}.json"


def klines_path(coin: str, interval: str) -> Path:
    return config.HIST_DIR / "cripto" / f"{coin}_{interval}.json"


def ls_path(coin: str) -> Path:
    return config.HIST_DIR / "flujo" / f"{coin}_ls_1h.json"


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning("Caché ilegible %s: %s", path, e)
        return None


def _write(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def is_fresh(obj: dict | None, now_ms: int | None = None) -> bool:
    return bool(obj and obj.get("rows")) and (now_ms or _now_ms()) - obj.get("fetched", 0) < MAX_AGE_S * 1000


def load_index(key: str) -> list[list]:
    """[[fecha 'AAAA-MM-DD' en la zona de la bolsa, open, high, low, close], ...]"""
    return (_read(index_path(key)) or {}).get("rows", [])


def load_klines(coin: str, interval: str) -> list[list]:
    """[[apertura ms UTC, open, high, low, close, volumen], ...]"""
    return (_read(klines_path(coin, interval)) or {}).get("rows", [])


def load_ls(coin: str) -> list[list]:
    """[[ms UTC, ratio de cuentas largo/corto], ...]"""
    return (_read(ls_path(coin)) or {}).get("rows", [])


# ---------- Lectores (funciones puras) ----------
def parse_yahoo(data: dict, tz: str) -> list[list]:
    """Velas diarias del gráfico de Yahoo con la fecha en la zona de la bolsa (Yahoo pone algunas a medianoche local,
    que en UTC cae el día anterior). Si la última vela llega sin cierre, se completa con el precio de la cabecera."""
    res = data["chart"]["result"][0]
    ts = res.get("timestamp") or []
    q = (res.get("indicators", {}).get("quote") or [{}])[0]
    meta = res.get("meta", {})
    z = ZoneInfo(tz)
    col = {k: q.get(k) or [] for k in ("open", "high", "low", "close")}
    by: dict[str, list] = {}
    for i, t in enumerate(ts):
        o, h, l, c = (col[k][i] if i < len(col[k]) else None for k in ("open", "high", "low", "close"))
        d = datetime.fromtimestamp(t, z).date().isoformat()
        if c is None and i == len(ts) - 1 and meta.get("regularMarketPrice") and meta.get("regularMarketTime"):
            if datetime.fromtimestamp(meta["regularMarketTime"], z).date().isoformat() == d:
                c = meta["regularMarketPrice"]
                h = h if h is not None else meta.get("regularMarketDayHigh")
                l = l if l is not None else meta.get("regularMarketDayLow")
        if c is None or c <= 0:
            continue
        r4 = lambda x: round(float(x), 4) if x is not None and x > 0 else None
        by[d] = [d, r4(o), r4(h), r4(l), round(float(c), 4)]
    return [by[d] for d in sorted(by)]


def drop_unfinished(rows: list[list], m: regions.Market, now: datetime) -> list[list]:
    """Quita la sesión de hoy si la bolsa aún no ha cerrado (su cierre sería provisional)."""
    if rows:
        d = date.fromisoformat(rows[-1][0])
        if regions.close_utc_on(m, d) + timedelta(minutes=20) > now:
            return rows[:-1]
    return rows


def parse_klines(raw: list, now_ms: int) -> list[list]:
    """Velas de Binance cerradas: [apertura ms, open, high, low, close, volumen]. La vela en curso se descarta."""
    return [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])]
            for k in raw if int(k[6]) < now_ms]


def parse_ls(raw: list) -> list[list]:
    return [[int(r["timestamp"]), float(r["longShortRatio"])] for r in raw]


# ---------- Red ----------
async def _get_json(session: aiohttp.ClientSession, url: str, params: dict | None = None,
                    headers: dict | None = None, tries: int = 4):
    wait = 1.5
    for k in range(tries):
        try:
            async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=40)) as r:
                r.raise_for_status()
                return await r.json(content_type=None)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if k == tries - 1:
                raise
            log.info("Reintento %s en %.0f s: %s", url, wait, str(e)[:120])
            await asyncio.sleep(wait)
            wait *= 2


async def _update_index(session: aiohttp.ClientSession, m: regions.Market) -> None:
    path = index_path(m.key)
    if is_fresh(await asyncio.to_thread(_read, path)):   # leer y escribir JSON va en un hilo: no frena el bucle
        return
    data = await _get_json(session, YAHOO.format(sym=m.index), {"interval": "1d", "range": "10y"},
                           {"User-Agent": BROWSER_UA, "Accept": "application/json"})
    rows = drop_unfinished(parse_yahoo(data, m.tz), m, datetime.now(timezone.utc))
    if len(rows) < 100:
        raise ValueError(f"Yahoo devolvió solo {len(rows)} sesiones")
    await asyncio.to_thread(_write, path, {"symbol": m.index, "market": m.key, "tz": m.tz, "fetched": _now_ms(),
                                           "rows": rows})
    log.info("Histórico %s (%s): %d sesiones hasta %s", m.index_name, m.index, len(rows), rows[-1][0])


async def _update_klines(session: aiohttp.ClientSession, coin: str, interval: str) -> None:
    """1d desde el principio y 1h de los últimos 730 días, paginando con startTime y limit=1000.
    Si ya hay caché, solo se pide lo que falta."""
    path = klines_path(coin, interval)
    old = await asyncio.to_thread(_read, path)
    if is_fresh(old):
        return
    now = _now_ms()
    step = STEP_MS[interval]
    floor = now - HOURLY_DAYS * 86400_000 if interval == "1h" else 0
    rows = {r[0]: r for r in (old or {}).get("rows", []) if r[0] >= floor}
    start = (max(rows) + step) if rows else (floor // step * step if floor else 0)
    pages = 0
    while start < now - step and pages < 60:
        raw = await _get_json(session, BINANCE, {"symbol": PAIRS[coin], "interval": interval,
                                                 "startTime": start, "limit": 1000})
        pages += 1
        if not raw:
            break
        for r in parse_klines(raw, now):
            rows[r[0]] = r
        last = int(raw[-1][0])
        if len(raw) < 1000 or last + step >= now:
            break
        start = last + step
        await asyncio.sleep(PAGE_PAUSE_S)
    out = [rows[t] for t in sorted(rows)]
    if not out:
        raise ValueError("Binance no devolvió velas")
    await asyncio.to_thread(_write, path, {"symbol": PAIRS[coin], "interval": interval, "fetched": _now_ms(),
                                           "rows": out})
    log.info("Velas %s %s: %d (%d páginas nuevas)", coin, interval, len(out), pages)


async def _update_ls(session: aiohttp.ClientSession, coin: str) -> None:
    """Binance solo guarda 30 días: se piden dos páginas de 500 h y se suman a lo ya guardado (crece con el tiempo)."""
    path = ls_path(coin)
    old = await asyncio.to_thread(_read, path)
    if is_fresh(old):
        return
    rows = {r[0]: r for r in (old or {}).get("rows", [])}
    params = {"symbol": PAIRS[coin], "period": "1h", "limit": 500}
    page = parse_ls(await _get_json(session, LS_URL, params))
    for r in page:
        rows[r[0]] = r
    if page:
        try:
            for r in parse_ls(await _get_json(session, LS_URL, {**params, "endTime": page[0][0] - 1}, tries=2)):
                rows[r[0]] = r
        except Exception as e:   # fuera de los 30 días Binance responde con error; basta con la primera página
            log.info("Ratio L/S %s: segunda página no disponible (%s)", coin, str(e)[:80])
    out = [rows[t] for t in sorted(rows)]
    if not out:
        raise ValueError("Binance no devolvió el ratio largo/corto")
    await asyncio.to_thread(_write, path, {"symbol": PAIRS[coin], "period": "1h", "fetched": _now_ms(), "rows": out})


async def ensure_history(session: aiohttp.ClientSession) -> dict:
    """Descarga lo que falte o esté caducado (más de 20 h). Un fallo de una fuente no para las demás.
    Devuelve el resumen de datos disponibles (bloque «data» del contrato 3.1) más los errores."""
    errors: list[str] = []
    sem = asyncio.Semaphore(4)

    async def guard(name: str, coro) -> None:
        async with sem:
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Histórico %s: %s", name, e)
                errors.append(f"{name}: {str(e)[:160]}")

    tasks = [guard(m.index, _update_index(session, m)) for m in regions.MARKETS.values() if m.index]
    for c in config.COINS:
        tasks += [guard(f"{c} 1d", _update_klines(session, c, "1d")), guard(f"{c} 1h", _update_klines(session, c, "1h")),
                  guard(f"{c} largo/corto", _update_ls(session, c))]
    await asyncio.gather(*tasks)
    out = await asyncio.to_thread(summary)
    out["errors"] = errors
    return out


def summary() -> dict:
    """Qué hay en caché: primera fecha, última fecha y número de filas por serie."""
    day = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).date().isoformat()
    out: dict = {"indices": {}, "coins": {}, "coins_1h": {}, "flow": {}}
    for m in regions.MARKETS.values():
        rows = load_index(m.key) if m.index else []
        if rows:
            out["indices"][m.index] = [rows[0][0], rows[-1][0], len(rows)]
    for c in config.COINS:
        for key, rows in (("coins", load_klines(c, "1d")), ("coins_1h", load_klines(c, "1h")), ("flow", load_ls(c))):
            if rows:
                out[key][c] = [day(rows[0][0]), day(rows[-1][0]), len(rows)]
    return out
