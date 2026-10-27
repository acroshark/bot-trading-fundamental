"""Datos extra para investigar estrategias de apertura. Todo va a config.HIST_DIR (en M:), nunca a C:.

- 1m/<SIM>-1m-<AAAA-MM>.zip (+ -api.json del mes en curso): velas de 1 min de Binance al contado desde 2021-01, con
  EXACTAMENTE los nombres, el formato y la verificación de backtest_aperturas (se importan sus funciones; ese módulo no
  se ejecuta como programa). Desde 2025 los CSV de Binance vienen en microsegundos: bt.norm_ms los pasa a ms.
- sentimiento/fng.json: índice de miedo y codicia diario de alternative.me, [["AAAA-MM-DD", 0-100], ...].
- funding/<SIM>.json: funding de los perpetuos USDT de Binance, [[ms, tasa], ...] (cada 8 h; la tasa es por periodo).
- macro/eventos.json: IPC (CPI) y empleo (NFP) a las 08:30 ET y comunicados del FOMC a las 14:00 ET, 2021-2026, con
  la hora exacta en UTC (con el cambio de hora de Nueva York). Las fechas salen de bls.gov y federalreserve.gov.
- gdelt/<slug>.json: tono medio y número de artículos de GDELT DOC 2.0, {"query", "resolution", "series": [[ms, tono,
  volumen], ...]}. Cada punto es el INICIO de su tramo (un día UTC con resolución 1d): solo se conoce al acabar el tramo.
  GDELT corta enseguida (429): la descarga va por tramos, guarda tras cada uno, se para si la cortan y, al relanzarla,
  sigue donde lo dejó.
- cobertura.json: desde, hasta y huecos de cada fuente.

Sin mirar el futuro: usa las funciones *_known_at, que devuelven solo lo que ya se sabía en un instante dado.

Uso: python -m bot.historia_extra [--solo 1m,fng,funding,macro,gdelt] [--offline] [--informe]"""
import argparse
import asyncio
import bisect
import io
import json
import logging
import os
import sys
import time
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

from . import backtest_aperturas as bt
from . import config, history

log = logging.getLogger("historia_extra")

MIN = 60_000
H = 3600_000
DAY = 86400_000
START_1M = "2021-01"
FNG_URL = "https://api.alternative.me/fng/?limit=0&format=json"
FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
FUNDING_START = "2021-01-01"
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_START = "2021-01-01"
GDELT_PAUSE_S = 20.0         # GDELT pide como mucho 1 petición cada 5 s; en la práctica corta mucho antes
GDELT_TRIES = 6
GDELT_MAX_FAILS = 2          # tramos seguidos sin respuesta: GDELT nos ha cortado y se para (insistir lo alarga)
ET = ZoneInfo("America/New_York")
SYMS = bt.SYMS               # {"BTC": "BTCUSDT", ...}

# Consultas de GDELT (en inglés), en orden de prioridad: slug → consulta. Los OR van entre paréntesis, como pide la API.
GDELT_QUERIES = {
    "cripto": "(bitcoin OR crypto OR cryptocurrency)",
    "fed": "(\"federal reserve\" OR \"interest rates\")",
    "guerra": "(war OR missile OR invasion OR airstrike)",
    "aranceles": "(tariffs OR \"trade war\")",
    "china_eeuu": "china \"united states\" trade",
    "elecciones": "election",
}
# Resolución → (tamaño del tramo pedido, paso de la serie). Probado el 26-10-2026 con datos de 2021: un tramo de 2 días
# da puntos de 15 min, uno de 7 días da horas y uno de 90 días da días. Los tramos de 1 año o de 2021 a hoy siempre
# reciben el aviso de límite (429), así que se pide de 90 en 90 días. GDELT además corta mucho más de lo que dice: con
# 20-100 s entre intentos, solo pasa más o menos una petición por minuto.
GDELT_RES = {"1d": (90 * DAY, DAY), "1h": (7 * DAY, H)}
GDELT_MODES = ("timelinetone", "timelinevolraw")   # tono medio y número de artículos
GDELT_NOTE = ("GDELT DOC 2.0 sí da series desde 2021 (15 min con tramos de 2 días, horas con 7 días, días con 90 días), "
              "pero responde HTTP 429 «Please limit requests to one every 5 seconds or contact "
              "kalev.leetaru5@gmail.com for larger queries» casi siempre: el 26-10-2026, con 20-240 s entre intentos, "
              "pasó ~1 petición cada 2-5 min y los tramos de 1 año o de 2021 a hoy nunca pasaron. Faltan muchas "
              "peticiones (2 por tramo de 90 días y consulta): relanzar --solo gdelt, que sigue donde lo dejó. "
              "Ojo: GDELT revisa sus timelines hacia atrás, así que lo descargado hoy no es exactamente lo que se veía "
              "en su día; cada tramo se da por conocido al cerrar más 30 min (1 h si es horario o diario).")
GDELT_LAG_FAST_MS = 30 * MIN      # retraso de publicación con tramos de menos de 1 h
GDELT_LAG_MS = H                  # con tramos horarios o diarios

# Horarios de publicación supuestos y dónde se comprobaron por fuera (26-10-2026). El control control_futuro.py del
# laboratorio solo prueba la coherencia interna (usa los mismos márgenes); esto es la validación externa.
TIMING_SOURCES = {
    "fng": "alternative.me actualiza a las 00:00 UTC (campo time_until_update de https://api.alternative.me/fng/); "
           "se usa con 1 h de margen.",
    "funding": "fundingTime de https://fapi.binance.com/fapi/v1/fundingRate es el sello de la liquidación (+0-5 ms); "
               "se usa desde la liquidación y nunca la que coincide con la apertura.",
    "macro": "IPC y empleo a las 08:30 ET (bls.gov, archivo de publicaciones y calendario oficial, comprobadas una a "
             "una de 11-2023 a 10-2026, con los retrasos del cierre de 2025) y FOMC a las 14:00 ET "
             "(federalreserve.gov, 2024-2026); horas UTC con el cambio de hora de Nueva York.",
    "gdelt": "Cada punto es el inicio de su tramo: se conoce al cerrar el tramo más 30 min (1 h si es horario o "
             "diario). Las series se revisan hacia atrás.",
    "indices": "Cierres de bolsa con 15 min de margen sobre la hora de cierre (backtest.CLOSE_MARGIN_MS).",
}

# ---------------------------------------------------------------- calendario macro (fuentes oficiales)
# Fechas de publicación (MMDDAAAA) sacadas del nombre de cada publicación en el archivo oficial de la BLS
# (https://www.bls.gov/bls/news-release/cpi.htm y empsit.htm, enlaces /news.release/archives/<serie>_MMDDAAAA.htm) y,
# para lo que aún no ha salido, del calendario https://www.bls.gov/schedule/news_release/<serie>.htm. Cada publicación
# dice «embargoed until 8:30 a.m. (ET)». Octubre de 2025 no se publicó por el cierre del Gobierno (IPC) o salió junto a
# noviembre (empleo, 16-12-2025). Los comunicados del FOMC salen de
# https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm («For release at 2:00 p.m.» en cada comunicado).
# Entre 2021 y 2026 no hubo reuniones extraordinarias con decisión de tipos; la votación por escrito del 22-08-2025
# (10:00 ET) solo aprobó la nueva declaración de estrategia y no cuenta como decisión.
MACRO_SRC = {
    "CPI": ("08:30", "https://www.bls.gov/bls/news-release/cpi.htm"),
    "NFP": ("08:30", "https://www.bls.gov/bls/news-release/empsit.htm"),
    "FOMC": ("14:00", "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"),
}
# (mes de referencia AAAA-MM, fecha de publicación MMDDAAAA)
CPI_RELEASES = """
2020-12 01132021 2021-01 02102021 2021-02 03102021 2021-03 04132021 2021-04 05122021 2021-05 06102021
2021-06 07132021 2021-07 08112021 2021-08 09142021 2021-09 10132021 2021-10 11102021 2021-11 12102021
2021-12 01122022 2022-01 02102022 2022-02 03102022 2022-03 04122022 2022-04 05112022 2022-05 06102022
2022-06 07132022 2022-07 08102022 2022-08 09132022 2022-09 10132022 2022-10 11102022 2022-11 12132022
2022-12 01122023 2023-01 02142023 2023-02 03142023 2023-03 04122023 2023-04 05102023 2023-05 06132023
2023-06 07122023 2023-07 08102023 2023-08 09132023 2023-09 10122023 2023-10 11142023 2023-11 12122023
2023-12 01112024 2024-01 02132024 2024-02 03122024 2024-03 04102024 2024-04 05152024 2024-05 06122024
2024-06 07112024 2024-07 08142024 2024-08 09112024 2024-09 10102024 2024-10 11132024 2024-11 12112024
2024-12 01152025 2025-01 02122025 2025-02 03122025 2025-03 04102025 2025-04 05132025 2025-05 06112025
2025-06 07152025 2025-07 08122025 2025-08 09112025 2025-09 10242025 2025-11 12182025
2025-12 01132026 2026-01 02132026 2026-02 03112026 2026-03 04102026 2026-04 05122026 2026-05 06102026
2026-06 07142026 2026-07 08122026 2026-08 09112026 2026-09 10142026 2026-10 11102026 2026-11 12102026
"""
NFP_RELEASES = """
2020-12 01082021 2021-01 02052021 2021-02 03052021 2021-03 04022021 2021-04 05072021 2021-05 06042021
2021-06 07022021 2021-07 08062021 2021-08 09032021 2021-09 10082021 2021-10 11052021 2021-11 12032021
2021-12 01072022 2022-01 02042022 2022-02 03042022 2022-03 04012022 2022-04 05062022 2022-05 06032022
2022-06 07082022 2022-07 08052022 2022-08 09022022 2022-09 10072022 2022-10 11042022 2022-11 12022022
2022-12 01062023 2023-01 02032023 2023-02 03102023 2023-03 04072023 2023-04 05052023 2023-05 06022023
2023-06 07072023 2023-07 08042023 2023-08 09012023 2023-09 10062023 2023-10 11032023 2023-11 12082023
2023-12 01052024 2024-01 02022024 2024-02 03082024 2024-03 04052024 2024-04 05032024 2024-05 06072024
2024-06 07052024 2024-07 08022024 2024-08 09062024 2024-09 10042024 2024-10 11012024 2024-11 12062024
2024-12 01102025 2025-01 02072025 2025-02 03072025 2025-03 04042025 2025-04 05022025 2025-05 06062025
2025-06 07032025 2025-07 08012025 2025-08 09052025 2025-09 11202025 2025-11 12162025
2025-12 01092026 2026-01 02112026 2026-02 03062026 2026-03 04032026 2026-04 05082026 2026-05 06052026
2026-06 07022026 2026-07 08072026 2026-08 09042026 2026-09 10022026 2026-10 11062026 2026-11 12042026
"""
# Día del comunicado (segundo día de la reunión), AAAAMMDD.
FOMC_STATEMENTS = """
20210127 20210317 20210428 20210616 20210728 20210922 20211103 20211215
20220126 20220316 20220504 20220615 20220727 20220921 20221102 20221214
20230201 20230322 20230503 20230614 20230726 20230920 20231101 20231213
20240131 20240320 20240501 20240612 20240731 20240918 20241107 20241218
20250129 20250319 20250507 20250618 20250730 20250917 20251029 20251210
20260128 20260318 20260429 20260617 20260729 20260916 20261028 20261209
"""


# ---------------------------------------------------------------- utilidades

def _base(base: Path | None) -> Path:
    return Path(base) if base is not None else config.HIST_DIR


def _write_json(path: Path, obj) -> None:
    """Escritura atómica (archivo temporal y os.replace), como history._write pero para cualquier JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except ValueError as e:
        log.warning("JSON ilegible %s: %s", path, e)
        return None


def ms_to_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).date().isoformat()


def day_ms(day: str) -> int:
    d = date.fromisoformat(day)
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def _known(times: list[int], t_ms: int, lag_ms: int) -> int:
    """Índice del último dato con tiempo + lag ≤ t_ms (-1 si ninguno)."""
    return bisect.bisect_right(times, t_ms - lag_ms) - 1


def fng_path(base: Path | None = None) -> Path:
    return _base(base) / "sentimiento" / "fng.json"


def funding_path(sym: str, base: Path | None = None) -> Path:
    return _base(base) / "funding" / f"{_sym(sym)}.json"


def macro_path(base: Path | None = None) -> Path:
    return _base(base) / "macro" / "eventos.json"


def gdelt_path(slug: str, base: Path | None = None) -> Path:
    return _base(base) / "gdelt" / f"{slug}.json"


def coverage_path(base: Path | None = None) -> Path:
    return _base(base) / "cobertura.json"


def _sym(s: str) -> str:
    """'BTC' o 'BTCUSDT' → 'BTCUSDT'."""
    s = s.upper()
    return SYMS.get(s, s)


# ---------------------------------------------------------------- velas de 1 min

def months_since(start_ym: str, today: date) -> tuple[list[str], str]:
    """Meses completos desde start_ym hasta el mes anterior a today, y el mes en curso (con bt.month_list)."""
    y, m = int(start_ym[:4]), int(start_ym[5:])
    n = (today.year - y) * 12 + (today.month - m)
    return bt.month_list(today, max(n, 0))


async def ensure_1m(start_ym: str = START_1M, offline: bool = False) -> dict:
    """Descarga lo que falte con bt.ensure_data (suma SHA-256, tamaño y zip legible; el mes en curso por la API)."""
    months, current = months_since(start_ym, datetime.now(timezone.utc).date())
    return await bt.ensure_data(months, current, offline)


def scan_times(path: Path) -> tuple[list[int], int]:
    """Tiempos de apertura (ms) de un archivo de velas y cuántos venían en microsegundos."""
    path = Path(path)
    out, us = [], 0
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as z:
            with z.open(z.namelist()[0]) as f:
                for line in io.TextIOWrapper(f, encoding="ascii", errors="ignore"):
                    head = line.split(",", 1)[0].strip()
                    if not head.isdigit():
                        continue
                    t = int(head)
                    if t > 10 ** 14:
                        us += 1
                    out.append(bt.norm_ms(t))
    else:
        for r in (_read_json(path) or {}).get("rows") or []:
            out.append(int(r[0]))
    return out, us


def check_month(path: Path, ym: str) -> dict:
    """Comprueba un mes: unidades de tiempo, filas fuera del mes, duplicados, desorden y minutos que faltan."""
    times, us = scan_times(path)
    a, b = bt.month_bounds(ym)
    inside = [t for t in times if a <= t < b]
    uniq = sorted(set(inside))
    gaps = [(uniq[k - 1], uniq[k]) for k in range(1, len(uniq)) if uniq[k] - uniq[k - 1] > MIN]
    return {"month": ym, "file": Path(path).name, "rows": len(times), "us_rows": us,
            "units": "us" if us and us == len(times) else "mixto" if us else "ms",
            "outside": len(times) - len(inside), "dups": len(inside) - len(uniq),
            "unsorted": any(times[k] <= times[k - 1] for k in range(1, len(times))),
            "first": uniq[0] if uniq else None, "last": uniq[-1] if uniq else None,
            "gaps": [[g0, g1, (g1 - g0) // MIN - 1] for g0, g1 in gaps]}


def coverage_1m(start_ym: str = START_1M, today: date | None = None) -> dict:
    """Cobertura por símbolo: desde, hasta, velas, minutos que faltan, meses en µs y huecos de más de 5 min."""
    today = today or datetime.now(timezone.utc).date()
    months, current = months_since(start_ym, today)
    out = {}
    for coin, sym in SYMS.items():
        per, missing, gaps, first, last, rows = [], [], [], None, None, 0
        for ym in months + [current]:
            z, p = bt.zip_path(sym, ym), bt.part_path(sym, ym)
            path = z if z.exists() else p if p.exists() else None
            if path is None:
                missing.append(ym)
                continue
            c = check_month(path, ym)
            per.append(c)
            rows += c["rows"] - c["outside"] - c["dups"]
            first = c["first"] if first is None else first
            last = c["last"] if c["last"] is not None else last
            gaps += [g for g in c["gaps"] if g[2] > 5]
        for k in range(1, len(per)):   # huecos entre meses consecutivos
            if per[k - 1]["last"] and per[k]["first"] and per[k]["first"] - per[k - 1]["last"] > 6 * MIN:
                gaps.append([per[k - 1]["last"], per[k]["first"], (per[k]["first"] - per[k - 1]["last"]) // MIN - 1])
        span = (last - first) // MIN + 1 if first is not None else 0
        out[sym] = {"from": ms_to_day(first) if first else None,
                    "to": datetime.fromtimestamp(last / 1000, timezone.utc).isoformat(timespec="minutes") if last else None,
                    "bars": rows, "expected": span, "missing_min": span - rows,
                    "months": len(per), "missing_months": missing,
                    "us_months": [c["month"] for c in per if c["us_rows"]],
                    "bad": [c["month"] for c in per if c["outside"] or c["dups"] or c["unsorted"]],
                    "gaps_gt5": sorted(gaps, key=lambda g: -g[2])[:15], "n_gaps_gt5": len(gaps)}
    return out


# ---------------------------------------------------------------- miedo y codicia

def parse_fng(obj: dict) -> list[list]:
    """Respuesta de alternative.me → [["AAAA-MM-DD", valor], ...] ordenado, sin repetidos y solo valores 0-100.
    El sello de cada dato es la medianoche UTC del día al que corresponde."""
    err = (obj.get("metadata") or {}).get("error")
    if err:
        raise ValueError(f"alternative.me: {err}")
    by: dict[str, int] = {}
    for r in obj.get("data") or []:
        try:
            v, ts = int(r["value"]), int(r["timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= v <= 100:
            by[ms_to_day(ts * 1000)] = v
    return [[d, by[d]] for d in sorted(by)]


def load_fng(base: Path | None = None) -> list[list]:
    """[["AAAA-MM-DD", 0-100], ...] ordenado ([] si no hay archivo)."""
    rows = _read_json(fng_path(base)) or []
    return sorted(([str(d), int(v)] for d, v in rows), key=lambda r: r[0])


def fng_known_at(rows: list[list], t_ms: int, lag_ms: int = H) -> list | None:
    """Último dato publicado antes de t_ms. El del día D sale hacia las 00:00 UTC de D; por prudencia, 1 h después."""
    times = [day_ms(r[0]) for r in rows]
    i = _known(times, t_ms, lag_ms)
    return rows[i] if i >= 0 else None


async def fetch_fng(session: aiohttp.ClientSession, base: Path | None = None) -> int:
    rows = parse_fng(await history._get_json(session, FNG_URL))
    if len(rows) < 1000:
        raise ValueError(f"alternative.me devolvió solo {len(rows)} días")
    _write_json(fng_path(base), rows)
    log.info("Miedo y codicia: %d días (%s → %s)", len(rows), rows[0][0], rows[-1][0])
    return len(rows)


# ---------------------------------------------------------------- funding

def parse_funding(raw: list) -> list[list]:
    """Respuesta de /fapi/v1/fundingRate → [[ms, tasa], ...] ordenado y sin repetidos."""
    by = {}
    for r in raw or []:
        try:
            by[int(r["fundingTime"])] = float(r["fundingRate"])
        except (KeyError, TypeError, ValueError):
            continue
    return [[t, by[t]] for t in sorted(by)]


def load_funding(sym: str, base: Path | None = None) -> list[list]:
    """[[ms, tasa por periodo], ...] de 'BTC' o 'BTCUSDT' ([] si no hay archivo)."""
    rows = _read_json(funding_path(sym, base)) or []
    return sorted(([int(t), float(r)] for t, r in rows), key=lambda r: r[0])


def funding_known_at(rows: list[list], t_ms: int) -> list | None:
    """Último funding liquidado en t_ms o antes (la tasa se fija en el momento de la liquidación)."""
    i = _known([r[0] for r in rows], t_ms, 0)
    return rows[i] if i >= 0 else None


async def fetch_funding(session: aiohttp.ClientSession, sym: str, base: Path | None = None,
                        start: str = FUNDING_START) -> int:
    """Paginado con startTime y limit=1000; si ya hay archivo, solo lo que falta."""
    sym = _sym(sym)
    path = funding_path(sym, base)
    rows = {t: r for t, r in load_funding(sym, base)}
    t = (max(rows) + 1) if rows else day_ms(start)
    now = int(time.time() * 1000)
    for _ in range(100):
        raw = await history._get_json(session, FUNDING_URL, {"symbol": sym, "startTime": t, "limit": 1000})
        page = parse_funding(raw)
        for ts, r in page:
            rows[ts] = r
        if len(raw or []) < 1000 or not page or page[-1][0] >= now:
            break
        t = page[-1][0] + 1
        await asyncio.sleep(0.3)
    out = [[ts, rows[ts]] for ts in sorted(rows)]
    if not out:
        raise ValueError(f"Binance no devolvió funding de {sym}")
    _write_json(path, out)
    log.info("Funding %s: %d (%s → %s)", sym, len(out), ms_to_day(out[0][0]), ms_to_day(out[-1][0]))
    return len(out)


# ---------------------------------------------------------------- calendario macro

def et_ms(day: date, hhmm: str) -> int:
    """Hora de Nueva York (con su cambio de hora) → ms UTC."""
    h, m = (int(x) for x in hhmm.split(":"))
    return int(datetime(day.year, day.month, day.day, h, m, tzinfo=ET).timestamp() * 1000)


def _mdy(s: str) -> date:
    return date(int(s[4:]), int(s[:2]), int(s[2:4]))


def macro_events(now_ms: int | None = None, years: tuple[int, int] = (2021, 2026)) -> list[dict]:
    """Eventos de las tablas oficiales, ordenados por hora. «scheduled» marca lo que aún no ha salido (fecha del
    calendario oficial, que puede cambiar). «ref» es el mes de referencia del dato (IPC y empleo)."""
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    out = []
    for name, table in (("CPI", CPI_RELEASES), ("NFP", NFP_RELEASES)):
        tok = table.split()
        for ref, mdy in zip(tok[0::2], tok[1::2]):
            out.append({"name": name, "date": _mdy(mdy).isoformat(), "ref": ref, "t_et": MACRO_SRC[name][0]})
    for ymd in FOMC_STATEMENTS.split():
        d = date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:]))
        out.append({"name": "FOMC", "date": d.isoformat(), "ref": None, "t_et": MACRO_SRC["FOMC"][0]})
    res = []
    for e in out:
        d = date.fromisoformat(e["date"])
        if not years[0] <= d.year <= years[1]:
            continue
        t = et_ms(d, e["t_et"])
        res.append({"name": e["name"], "date": e["date"], "t_ms": t, "t_et": e["t_et"], "ref": e["ref"],
                    "scheduled": t > now_ms, "src": MACRO_SRC[e["name"]][1]})
    return sorted(res, key=lambda e: (e["t_ms"], e["name"]))


def build_macro(base: Path | None = None, now_ms: int | None = None) -> int:
    ev = macro_events(now_ms)
    _write_json(macro_path(base), ev)
    log.info("Calendario macro: %d eventos", len(ev))
    return len(ev)


def load_macro(base: Path | None = None, names: tuple[str, ...] | None = None) -> list[dict]:
    """[{"name": "CPI"|"NFP"|"FOMC", "date", "t_ms", ...}, ...] ordenado por t_ms ([] si no hay archivo)."""
    ev = _read_json(macro_path(base)) or []
    ev = [e for e in ev if not names or e["name"] in names]
    return sorted(ev, key=lambda e: (e["t_ms"], e["name"]))


MACRO_GAP_NOTE = ("Octubre de 2025: el IPC no se publicó y el empleo salió junto a noviembre (2025-12-16), por el cierre "
                  "del Gobierno de EE.UU.; el IPC de septiembre de 2025 se retrasó al 2025-10-24 y el empleo al "
                  "2025-11-20.")


def missing_refs(events: list[dict], name: str) -> list[str]:
    """Meses de referencia sin publicación propia entre el primero y el último de la serie."""
    refs = sorted(e["ref"] for e in events if e["name"] == name and e.get("ref"))
    if not refs:
        return []
    have, out = set(refs), []
    y, m = int(refs[0][:4]), int(refs[0][5:])
    while f"{y:04d}-{m:02d}" <= refs[-1]:
        if f"{y:04d}-{m:02d}" not in have:
            out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def macro_between(events: list[dict], a_ms: int, b_ms: int) -> list[dict]:
    """Eventos con a_ms ≤ t_ms < b_ms (por ejemplo, los que caen dentro de una ventana de apertura)."""
    return [e for e in events if a_ms <= e["t_ms"] < b_ms]


# ---------------------------------------------------------------- GDELT

def gdelt_ms(s: str) -> int:
    """'20210101T000000Z' → ms UTC."""
    return int(datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp() * 1000)


def gdelt_dt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y%m%d%H%M%S")


def parse_gdelt(obj: dict) -> dict[int, float]:
    """Primera serie de un timeline de GDELT → {ms: valor}."""
    out = {}
    for s in (obj or {}).get("timeline") or []:
        for p in s.get("data") or []:
            try:
                out[gdelt_ms(p["date"])] = float(p["value"])
            except (KeyError, TypeError, ValueError):
                continue
        break
    return out


def merge_gdelt(tone: dict[int, float], vol: dict[int, float]) -> list[list]:
    """[[ms, tono, volumen], ...]; un lado que falte va como None."""
    return [[t, tone.get(t), vol.get(t)] for t in sorted(set(tone) | set(vol))]


def gdelt_chunks(a_ms: int, b_ms: int, size_ms: int) -> list[tuple[int, int]]:
    out, t = [], a_ms
    while t < b_ms:
        out.append((t, min(t + size_ms, b_ms)))
        t += size_ms
    return out


def gdelt_text_is_limit(txt: str) -> bool:
    return "limit requests" in txt.lower()


class Pacer:
    """Como mucho una petición cada `pause` segundos (compartido por todas las consultas)."""

    def __init__(self, pause: float):
        self.pause, self.last, self.lock = pause, 0.0, asyncio.Lock()

    async def wait(self) -> None:
        async with self.lock:
            dt = self.last + self.pause - time.monotonic()
            if dt > 0:
                await asyncio.sleep(dt)
            self.last = time.monotonic()


async def _gdelt_get(session: aiohttp.ClientSession, pacer: Pacer, query: str, mode: str, a: int, b: int) -> dict:
    """Una petición con reintentos: el aviso de límite (429) espera cada vez más."""
    params = {"query": query, "mode": mode, "format": "json", "STARTDATETIME": gdelt_dt(a),
              "ENDDATETIME": gdelt_dt(b)}
    wait = 30.0
    for k in range(GDELT_TRIES):
        await pacer.wait()
        try:
            async with session.get(GDELT_URL, params=params, timeout=aiohttp.ClientTimeout(total=120)) as r:
                txt = await r.text()
                if r.status == 429 or gdelt_text_is_limit(txt):
                    raise IOError(f"HTTP {r.status}: {' '.join(txt.split())[:110]}")
                r.raise_for_status()
                if not txt.strip():
                    return {}
                return json.loads(txt)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if k == GDELT_TRIES - 1:
                raise
            log.info("GDELT %s %s: %s; reintento en %.0f s", mode, gdelt_dt(a)[:8], str(e)[:80], wait)
            await asyncio.sleep(wait)
            wait = min(wait * 1.5, 240)
    return {}


def load_gdelt(slug: str, base: Path | None = None) -> dict:
    """{"query", "resolution", "step_ms", "series": [[ms del inicio del tramo, tono, volumen], ...], ...} o {}."""
    obj = _read_json(gdelt_path(slug, base)) or {}
    if obj.get("series"):
        obj["series"] = sorted(obj["series"], key=lambda r: r[0])
    return obj


def gdelt_lag(step_ms: int) -> int:
    """Retraso de publicación tras cerrar un tramo: 30 min con tramos de menos de 1 h; 1 h con horarios o diarios."""
    return GDELT_LAG_FAST_MS if step_ms < H else GDELT_LAG_MS


def gdelt_known_at(obj: dict, t_ms: int) -> list | None:
    """Último tramo YA CERRADO Y PUBLICADO en t_ms (inicio + paso + retraso ≤ t_ms): el dato del día D solo vale desde
    la 01:00 UTC del día D+1."""
    s = obj.get("series") or []
    step = obj.get("step_ms") or DAY
    i = _known([r[0] for r in s], t_ms, step + gdelt_lag(step))
    return s[i] if i >= 0 else None


async def fetch_gdelt(session: aiohttp.ClientSession, pacer: Pacer, slug: str, query: str, res: str = "1d",
                      base: Path | None = None, start: str = GDELT_START, end_ms: int | None = None) -> dict:
    """Descarga por tramos y guarda tras cada uno (se puede cortar y seguir). Devuelve el resumen; «blocked» si GDELT
    no contesta en GDELT_MAX_FAILS tramos seguidos (se para para no alargar el corte)."""
    size, step = GDELT_RES[res]
    path = gdelt_path(slug if res == "1d" else f"{slug}-{res}", base)
    old = _read_json(path) or {}
    if old.get("query") != query or old.get("resolution") != res:
        old = {}
    done = {tuple(c) for c in old.get("chunks_done") or []}
    # cada modo se guarda por separado: si falla el volumen, el tono ya bajado no se vuelve a pedir
    mdone = {m: {tuple(c) for c in (old.get("modes_done") or {}).get(m, [])} | done for m in GDELT_MODES}
    series = {m: {r[0]: r[1 + k] for r in old.get("series") or [] if r[1 + k] is not None}
              for k, m in enumerate(GDELT_MODES)}
    end_ms = end_ms or (int(time.time() * 1000) // DAY * DAY)
    errors, fails, blocked = [], 0, False
    chunks = gdelt_chunks(day_ms(start), end_ms, size)
    last = chunks[-1] if chunks else None

    def save() -> None:
        _write_json(path, {"query": query, "resolution": res, "step_ms": step, "source": GDELT_URL,
                           "modes": list(GDELT_MODES), "fetched": int(time.time() * 1000),
                           "chunks_done": sorted(done), "modes_done": {m: sorted(mdone[m]) for m in GDELT_MODES},
                           "series": merge_gdelt(*(series[m] for m in GDELT_MODES))})

    # el último tramo se vuelve a pedir (puede haber crecido)
    for a, b in [c for c in chunks if c not in done or c == last]:
        try:
            for mode in GDELT_MODES:
                if (a, b) in mdone[mode] and (a, b) != last:
                    continue
                pts = parse_gdelt(await _gdelt_get(session, pacer, query, mode, a, b))
                # el punto final de un tramo es el inicio del siguiente: se queda solo dentro de [a, b)
                series[mode].update({k: x for k, x in pts.items() if a <= k < b})
                mdone[mode].add((a, b))
                save()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("GDELT %s %s-%s: %s", slug, gdelt_dt(a)[:8], gdelt_dt(b)[:8], e)
            errors.append(f"{gdelt_dt(a)[:8]}-{gdelt_dt(b)[:8]}: {str(e)[:160]}")
            fails += 1
            if fails >= GDELT_MAX_FAILS:
                blocked = True
                break
            continue
        fails = 0
        done.add((a, b))
        save()
    obj = _read_json(path) or {}
    if obj:
        obj["errors"] = errors
        obj["pending"] = len([c for c in chunks if c not in done])
        _write_json(path, obj)
    log.info("GDELT %s (%s): %d puntos, %d tramos con error%s", slug, res, len(obj.get("series") or []), len(errors),
             " (GDELT nos ha cortado: se para)" if blocked else "")
    return {"points": len(obj.get("series") or []), "errors": errors, "blocked": blocked,
            "pending": len([c for c in chunks if c not in done])}


# ---------------------------------------------------------------- cobertura

def _day_gaps(days: list[str], step_days: int = 1) -> list[list]:
    """Huecos en una lista ordenada de fechas: [[última con dato, siguiente con dato, días que faltan], ...]."""
    out = []
    for k in range(1, len(days)):
        d0, d1 = date.fromisoformat(days[k - 1]), date.fromisoformat(days[k])
        if (d1 - d0).days > step_days:
            out.append([days[k - 1], days[k], (d1 - d0).days - 1])
    return out


def _ms_gaps(times: list[int], step: int, tol: float = 1.5) -> list[list]:
    out = []
    for k in range(1, len(times)):
        if times[k] - times[k - 1] > step * tol:
            out.append([ms_to_day(times[k - 1]), ms_to_day(times[k]), round((times[k] - times[k - 1]) / step) - 1])
    return out


def coverage(base: Path | None = None, with_1m: bool = True) -> dict:
    """Desde, hasta, número de datos y huecos de cada fuente."""
    out: dict = {"generated": int(time.time() * 1000)}
    if with_1m and base is None:
        out["1m"] = coverage_1m()
    rows = load_fng(base)
    out["fng"] = {"from": rows[0][0] if rows else None, "to": rows[-1][0] if rows else None, "n": len(rows),
                  "gaps": _day_gaps([r[0] for r in rows])}
    out["funding"] = {}
    for sym in SYMS.values():
        f = load_funding(sym, base)
        ts = [r[0] for r in f]
        # el periodo de funding es de 8 h (algunos símbolos lo acortan en momentos de estrés)
        out["funding"][sym] = {"from": ms_to_day(ts[0]) if ts else None, "to": ms_to_day(ts[-1]) if ts else None,
                               "n": len(ts), "gaps": _ms_gaps(ts, 8 * H),
                               "short_periods": sum(1 for k in range(1, len(ts)) if ts[k] - ts[k - 1] < 7 * H)}
    ev = load_macro(base)
    now = int(time.time() * 1000)
    out["macro"] = {"n": len(ev), "from": ev[0]["date"] if ev else None, "to": ev[-1]["date"] if ev else None,
                    "by_name": {n: sum(1 for e in ev if e["name"] == n) for n in ("CPI", "NFP", "FOMC")},
                    "scheduled": sum(1 for e in ev if e["t_ms"] > now),
                    "missing": [f"{n} {ym}" for n in ("CPI", "NFP") for ym in missing_refs(ev, n)],
                    "note": MACRO_GAP_NOTE}
    out["gdelt"] = {}
    out["gdelt_note"] = GDELT_NOTE
    out["timing_sources"] = TIMING_SOURCES
    for slug in GDELT_QUERIES:
        for res in GDELT_RES:
            g = load_gdelt(slug if res == "1d" else f"{slug}-{res}", base)
            if not g:
                continue
            s = g.get("series") or []
            ts = [r[0] for r in s]
            out["gdelt"][slug if res == "1d" else f"{slug}-{res}"] = {
                "query": g.get("query"), "resolution": g.get("resolution"), "n": len(s),
                "from": ms_to_day(ts[0]) if ts else None, "to": ms_to_day(ts[-1]) if ts else None,
                "tone_missing": sum(1 for r in s if r[1] is None), "vol_missing": sum(1 for r in s if r[2] is None),
                "tone_to": max((ms_to_day(r[0]) for r in s if r[1] is not None), default=None),
                "vol_to": max((ms_to_day(r[0]) for r in s if r[2] is not None), default=None),
                "gaps": _ms_gaps(ts, g.get("step_ms") or DAY)[:20], "chunks_done": len(g.get("chunks_done") or []),
                "chunks_pending": len(gdelt_chunks(day_ms(GDELT_START), now // DAY * DAY, GDELT_RES[res][0]))
                - len(g.get("chunks_done") or []), "errors": g.get("errors") or []}
    return out


def coverage_text(cov: dict) -> list[str]:
    """Resumen en español."""
    L = ["Cobertura de los datos extra (config.HIST_DIR):"]
    for sym, c in (cov.get("1m") or {}).items():
        bars = f"{c['bars']:,}".replace(",", ".")
        L.append(f"- Velas 1 min {sym}: {c['from']} → {c['to']}, {bars} velas, faltan {c['missing_min']} min "
                 f"({c['n_gaps_gt5']} huecos de más de 5 min), {len(c['us_months'])} meses venían en µs (pasados a ms), "
                 f"meses que faltan: {c['missing_months'] or 'ninguno'}, con errores: {c['bad'] or 'ninguno'}")
    f = cov.get("fng") or {}
    L.append(f"- Miedo y codicia: {f.get('from')} → {f.get('to')}, {f.get('n')} días, {len(f.get('gaps') or [])} huecos"
             + (f" ({', '.join(f'{g[0]}→{g[1]}: {g[2]} d' for g in f['gaps'][:6])})" if f.get("gaps") else ""))
    for sym, c in (cov.get("funding") or {}).items():
        L.append(f"- Funding {sym}: {c['from']} → {c['to']}, {c['n']} liquidaciones, {len(c['gaps'])} huecos, "
                 f"{c['short_periods']} periodos de menos de 8 h")
    m = cov.get("macro") or {}
    L.append(f"- Macro EE.UU.: {m.get('n')} eventos {m.get('from')} → {m.get('to')} {m.get('by_name')}, "
             f"{m.get('scheduled')} aún programados; meses sin publicación propia: "
             f"{', '.join(m.get('missing') or []) or 'ninguno'}")
    for slug, g in (cov.get("gdelt") or {}).items():
        L.append(f"- GDELT {slug} ({g['resolution']}): {g['from']} → {g['to']} (tono hasta {g['tone_to']}, volumen "
                 f"hasta {g['vol_to']}), {g['n']} puntos, {len(g['gaps'])} huecos, {g['chunks_done']} tramos completos "
                 f"y {g['chunks_pending']} pendientes, {len(g['errors'])} tramos con error en la última pasada")
    missing = [q for q in GDELT_QUERIES if q not in (cov.get("gdelt") or {})]
    if missing:
        L.append(f"- GDELT sin descargar: {', '.join(missing)}")
    return L


# ---------------------------------------------------------------- programa

async def run(want: set[str], offline: bool, gdelt_res: str = "1d") -> dict:
    errors: dict[str, list[str]] = {}
    if "1m" in want:
        res = await ensure_1m(offline=offline)
        errors["1m"] = res["errors"]
    if "macro" in want:
        build_macro()
    if offline:
        return errors
    async with aiohttp.ClientSession(headers={"User-Agent": config.USER_AGENT}) as session:
        if "fng" in want:
            try:
                await fetch_fng(session)
            except Exception as e:
                log.warning("Miedo y codicia: %s", e)
                errors["fng"] = [str(e)[:200]]
        if "funding" in want:
            for sym in SYMS.values():
                try:
                    await fetch_funding(session, sym)
                except Exception as e:
                    log.warning("Funding %s: %s", sym, e)
                    errors.setdefault("funding", []).append(f"{sym}: {str(e)[:200]}")
        if "gdelt" in want:
            pacer = Pacer(GDELT_PAUSE_S)
            for slug, q in GDELT_QUERIES.items():
                r = await fetch_gdelt(session, pacer, slug, q, gdelt_res)
                if r["errors"]:
                    errors.setdefault("gdelt", []).extend(f"{slug}: {x}" for x in r["errors"])
                if r["blocked"]:
                    errors.setdefault("gdelt", []).append("GDELT nos ha cortado: se para; vuelve a lanzarlo más tarde "
                                                          "(sigue donde lo dejó)")
                    break
    return errors


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m bot.historia_extra", description="Datos extra para el backtest")
    ap.add_argument("--solo", default="1m,fng,funding,macro,gdelt", help="fuentes separadas por comas")
    ap.add_argument("--gdelt-res", default="1d", choices=list(GDELT_RES), help="resolución de GDELT (1d o 1h)")
    ap.add_argument("--offline", action="store_true", help="sin red: solo la caché y el calendario macro")
    ap.add_argument("--informe", action="store_true", help="solo el informe de cobertura")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not a.informe:
        want = {x.strip() for x in a.solo.split(",") if x.strip()}
        errs = asyncio.run(run(want, a.offline, a.gdelt_res))
        print("Errores:", {k: v for k, v in errs.items() if v} or "ninguno")
    cov = coverage()
    _write_json(coverage_path(), cov)
    out = "\n".join(coverage_text(cov))
    try:
        print(out)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((out + "\n").encode("utf-8"))
    print(f"Informe en {coverage_path()}")


if __name__ == "__main__":
    main()
