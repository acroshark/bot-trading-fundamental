"""Vigilancia en vivo: prueba en avance (forward test), SIN operar, de las estrategias congeladas del laboratorio.

Con las velas de 1 min de Binance al contado que van llegando (públicas, como bot/apertura.py; las nuevas se guardan en
la caché de config.HIST_DIR/1m con el formato de backtest_aperturas, el mes en curso en <SIM>-1m-<AAAA-MM>-api.json) y
las MISMAS funciones del laboratorio (bot/laboratorio.py: aperturas reales, características sin mirar al futuro,
entradas, confirmaciones, horquilla pura con costes y una posición por moneda), en cada apertura se calcula si cada
estrategia vigilada habría entrado, con qué horquilla, y la operación virtual se resuelve al tocar el stop o el objetivo
(o al acabar su duración: la misma apertura del día siguiente, 24 h o los 7 días de seguridad).

- Qué se vigila: las robustas y prometedoras de la lista congelada (BACKTEST_DIR/laboratorio-congelado-2026-10-26.json)
  y las de laboratorio.json, y de cada una que no dure 7 días, su «variante del usuario» aparte (la misma sin tope de
  tiempo, solo los 7 días de seguridad: la regla que opera el paper). Solo las que cumplen las reglas del usuario (stop
  ≥ 1,5 %, ≥ 2R y objetivo ≥ 3 % neto de comisión y deslizamiento de ida y vuelta). Cuentan desde la medianoche de
  Madrid siguiente a su congelación (el único fuera de muestra virgen); lo anterior se calcula como calentamiento
  (posiciones abiertas, aperturas previas), se guarda marcado como «previa» y no cuenta.
- Costes: los del laboratorio (contado). Los cortos van como perpetuos: comisión FEE_PERP por lado y el funding
  liquidado mientras están abiertos, como laboratorio.short_side. R = resultado neto / stop.
- Una apertura se evalúa cuando hay 4 h de velas tras ella (laboratorio.coin_tables las pide); cada operación lleva su
  hora real de entrada y de salida.
- Tabla SQLite propia «vigilancia» (una fila por estrategia, apertura y moneda: un reinicio no duplica nada); una
  operación cerrada ya no se toca (salvo el funding de un corto aún sin liquidar del todo). Estado del bus «vigilancia»
  cada minuto si cambia y GET /api/vigilancia (docs/04-contratos.md).
- El cálculo va en un hilo, solo cuando llegan velas nuevas y con velas de las 4 monedas; el funding va en una tarea
  aparte. Un fallo de datos o de cálculo nunca para el bucle.
NADA de esto opera ni mueve dinero (ni siquiera ficticio): son operaciones virtuales para comparar con el backtest."""
import asyncio
import hashlib
import json
import logging
import os
import statistics
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
import numpy as np

from . import apertura, config, historia_extra, history
from . import backtest_aperturas as bt
from . import laboratorio as L
from .models import now_ms

log = logging.getLogger("vigilancia")

MIN, H, DAY = L.MIN, L.H, L.DAY
MAD = ZoneInfo("Europe/Madrid")
ENABLED = os.environ.get("BOT_VIGILANCIA", "1") != "0"
FROZEN = os.environ.get("BOT_VIGILANCIA_CONGELADO", "laboratorio-congelado-2026-10-26.json")
LAB_FILE = "laboratorio.json"
TICK_S = 60
KEEP_D = 9            # días de aperturas que se recalculan en cada ronda (una operación dura como mucho 7)
WARM_D = 8            # calentamiento antes del inicio: posiciones que ya estarían abiertas
PRE_OPEN_D = 5        # aperturas anteriores que hacen falta para las características (apertura anterior, misma ayer)
LOOKBACK_D = 56       # velas antes de la primera apertura (ATR de 30 días, medias de BTC de 50 días, rvol)
AHEAD_D = 8           # aperturas futuras del calendario («hasta la misma apertura del día siguiente»)
MAX_BACK_D = 120      # tras un apagón largo, como mucho se recalcula esto
HOLE_D = 21           # huecos de la caché que se rellenan al arrancar (los que afectan a las aperturas vigiladas)
PAGE = 1000
MAX_PAGES = 200       # páginas por moneda y ronda (un hueco largo se rellena en varias rondas)
PAGE_PAUSE_S = 0.1
PERSIST_S = 900
SETTLE_MS = 5000      # una vela de Binance cuenta como cerrada 5 s después de su cierre (el reloj del PC puede adelantar)
REFETCH_MIN = 2       # en cada ronda se vuelven a pedir los últimos minutos; a la caché, solo las de hace más
FUNDING_S = 4 * 3600
FUNDING_WAIT_S = 60
STALE_MS = 10 * MIN
ERR_LOG_S = 300
KEEP_OPENINGS = 400
BIG = 2 ** 62
EVAL_MIN = L.POST_BARS + 1  # minutos de velas tras la apertura que pide el laboratorio
WHY = ("stop", "objetivo", "tiempo", "abierta")
MIN_NET_PCT = 3.0     # regla del usuario: objetivo de al menos un 3 % neto de comisión y deslizamiento de ida y vuelta
VARIANT_DUR = "7d"    # variante del usuario: sin tope de tiempo, solo los 7 días de seguridad
# Cifras del laboratorio de las variantes que no están en laboratorio.json: réplica aparte del 27-10-2026 con
# laboratorio.build_lab (mismo periodo, corte y costes que la pasada congelada; la versión «sig» da exactamente sus 278
# operaciones y +0,1402 R). NO forman parte de la lista congelada ni pasaron sus filtros: solo sirven para comparar.
VARIANT_REF = {
    "ap_mom:15:0,2|rvol:1,5|todos|a_favor|todas|todas|todos|t:30|3|3|7d": {
        "oos": {"trades": 202, "mean_R": 0.3459, "win_rate": 0.4109, "mean_ret_pct": 1.0378, "long_share": 0.4208},
        "sensitivity": {"cortos": {"mean_R_perp": 0.5626}},
        "period": {"split": "2025-08-16", "to": "2026-10-23"},
        "fuente": "réplica aparte con el laboratorio (no congelada, sin validar)"},
}
AVISO = ("Operaciones VIRTUALES: no se compra ni se vende nada y no hay dinero, ni siquiera ficticio. Es la prueba en "
         "avance de las estrategias congeladas del laboratorio, con sus mismas reglas y costes, para comparar lo que "
         "pasa en vivo con su backtest. No es una ventaja demostrada.")
COSTES = (f"Costes del laboratorio: {L._g(L.FEE * 100)} % de comisión por lado en contado, {L._g(L.SLIP * 1e4)} pb de "
          f"deslizamiento por lado, {L._g(L.STOP_EXTRA * 1e4)} pb más en los stops de BTC y ETH y "
          f"{L._g(L.STOP_EXTRA_ALT * 1e4)} pb en los de SOL y XRP; el objetivo solo cuenta si el precio lo supera en "
          f"{L._g(L.TP_THROUGH * 1e4)} pb. Los cortos van como perpetuos: {L._g(L.FEE_PERP * 100)} % de comisión por "
          "lado y el funding liquidado mientras están abiertos.")
CONFS = ("ninguna", "vwap", "rvol", "momento", "ruptura")
SCHEMA = """
CREATE TABLE IF NOT EXISTS vigilancia(id TEXT PRIMARY KEY, estrategia TEXT, apertura TEXT, moneda TEXT, lado TEXT,
  entrada_t INTEGER, entrada REAL, stop REAL, objetivo REAL, salida_t INTEGER, salida REAL, R REAL, pct REAL,
  motivo TEXT, cuerpo TEXT);
CREATE INDEX IF NOT EXISTS vigilancia_estrategia ON vigilancia(estrategia, entrada_t);
CREATE TABLE IF NOT EXISTS vigilancia_estado(clave TEXT PRIMARY KEY, valor TEXT);
"""
COLS = ("id", "estrategia", "apertura", "moneda", "lado", "entrada_t", "entrada", "stop", "objetivo", "salida_t",
        "salida", "R", "pct", "motivo", "cuerpo")


# ---------------------------------------------------------------- estrategias vigiladas

def _val(s: str):
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return s


def net_target_pct(st: dict) -> float:
    """Objetivo en % menos la comisión y el deslizamiento de ida y vuelta en contado (el caso más caro)."""
    return st["sl"] * st["r"] - 2 * (L.FEE + L.SLIP) * 100


def parse_id(sid: str) -> dict | None:
    """Identificador canónico del laboratorio (laboratorio.skey) → estrategia completa; None si no es válido o no
    cumple las reglas del usuario (stop ≥ 1,5 %, ≥ 2R, horquilla pura y objetivo ≥ 3 % neto)."""
    p = str(sid or "").split("|")
    if len(p) != 11:
        return None
    tup = lambda x: tuple(_val(v) for v in x.split(":"))
    try:
        st = {"src": tup(p[0]), "conf": tup(p[1]), "vol": p[2], "btc": p[3], "win": p[4], "coins": p[5], "dia": p[6],
              "entry": tup(p[7]), "sl": float(p[8].replace(",", ".")), "r": float(p[9].replace(",", ".")), "dur": p[10]}
    except ValueError:
        return None
    fam = next((f for f, srcs in L.SOURCES.items() if st["src"] in srcs), None)
    ok = (fam is not None and st["conf"][0] in CONFS and st["vol"] in L.VOL_W and st["btc"] in L.BTC_W
          and st["win"] in L.WIN_SETS and st["coins"] in L.COIN_SETS and st["dia"] in L.DIA_W
          and st["entry"] in L.KEY_IDX and st["sl"] in L.SL_ALL and st["r"] in L.R_ALL and L.rules_ok(st)
          and net_target_pct(st) >= MIN_NET_PCT - 1e-9)
    if not ok:
        return None
    st["family"] = fam
    if L.skey(st) != sid:
        return None
    st["id"] = sid
    return st


def next_midnight(day) -> int | None:
    """Medianoche de Madrid del día siguiente a day (AAAA-MM-DD): desde ahí cuenta la vigilancia."""
    try:
        d = date.fromisoformat(str(day)[:10])
    except (TypeError, ValueError):
        return None
    return int(datetime.combine(d + timedelta(days=1), dtime(0), tzinfo=MAD).timestamp() * 1000)


def paper_id() -> str | None:
    """Estrategia del laboratorio que opera el paper en modo impulso: la del preset IMPULSO con sus 7 días de
    seguridad (None si apertura.py no la declara así). El preset SEIS («6 diarias», lab None) no se vigila: su umbral 0
    no está en la rejilla del laboratorio (ap_mom: 0,2, 0,5 y 1 %) ni el laboratorio tiene su tope de 6 entradas al día
    ni el veto de noticias fiables; se mide solo en paper (y su réplica de 3 años, en seis_diarias_resumen.json)."""
    ref, imp = getattr(apertura, "LAB_REF", None), getattr(apertura, "IMPULSO", None) or {}
    if not isinstance(ref, str) or imp.get("max_hold_h") != 168 or not paper_es_impulso():
        return None
    return ref.rsplit("|", 1)[0] + "|" + VARIANT_DUR


def paper_es_impulso(path=None) -> bool:
    """¿Opera el paper el preset IMPULSO? No si estrategia_activa.json (evolucion.leer_estrategia_activa) pone otra
    estrategia: «6 diarias» (scripts/activar_estrategia.py) o una versión ajustada por la evolución. Entonces ninguna
    vigilada es «la regla que opera el paper». Sin archivo, o si no se puede leer, el preset de siempre."""
    try:
        from . import evolucion   # perezoso: la evolución es otro módulo grande
        act = evolucion.leer_estrategia_activa(path or config.DATA_DIR / evolucion.ARCHIVO)
        over = evolucion.params_motor(act)
        if not over:
            return True
        p = apertura.impulse_setup({k: v for k, v in over.items() if k != "estrategia"})[0]
        return apertura.preset_of(p) == "impulso"
    except Exception:
        log.exception("Vigilancia: no pude leer la estrategia activa; se supone el preset IMPULSO")
        return True


def watch_item(sid: str, desde: int, nombre: str | None = None, clase: str = "prometedora",
               origen: str = "congelada", ref: dict | None = None, period: dict | None = None,
               variante_de: str | None = None) -> dict | None:
    """Una estrategia vigilada con su cifra del backtest fuera de muestra (de laboratorio.json o, en una variante,
    de VARIANT_REF), si la hay."""
    st = parse_id(sid)
    if st is None:
        return None
    ref = ref or {}
    oos = ref.get("oos") or {}
    cortos = (ref.get("sensitivity") or {}).get("cortos") or {}
    per = ref.get("period") or period or {}
    btx = None
    if oos.get("trades"):
        btx = {"operaciones": oos.get("trades"), "R_medio": oos.get("mean_R"), "ganadoras": oos.get("win_rate"),
               "pct_medio": oos.get("mean_ret_pct"), "largos": oos.get("long_share"),
               "R_medio_cortos_perp": cortos.get("mean_R_perp"),
               "periodo": f"{per['split']} a {per['to']}" if per.get("split") and per.get("to") else None}
        if ref.get("fuente"):
            btx["fuente"] = ref["fuente"]
    return {"id": sid, "nombre": nombre or ref.get("name") or L.describe(st), "clase": clase, "origen": origen,
            "desde": int(desde), "st": st, "variante_de": variante_de, "en_paper": sid == paper_id(),
            "reglas": {"sl_pct": st["sl"], "r": st["r"], "tp_pct": round(st["sl"] * st["r"], 4), "dur": st["dur"],
                       "dur_texto": L.DUR_TEXT.get(st["dur"]), "neto_pct": round(net_target_pct(st), 4)},
            "bt": btx}


def load_watch() -> tuple[list[dict], dict]:
    """Robustas y prometedoras de la lista congelada y de laboratorio.json (sin repetir). Las de la lista congelada
    cuentan desde el día siguiente a su fecha; las que solo están en laboratorio.json, desde el día siguiente a la
    congelación de esa pasada (si es posterior). Tras cada una que no dure 7 días, su variante del usuario (código
    «E2-7d»): la misma sin tope de tiempo, desde el mismo día; la de «sig» sigue siendo la prueba de la hipótesis
    congelada."""
    base = config.BACKTEST_DIR
    fz = L._read_json(base / FROZEN)
    lab = L._read_json(base / LAB_FILE)
    fz = fz if isinstance(fz, dict) else {}
    lab = lab if isinstance(lab, dict) else {}
    info: dict[str, dict] = {}
    for key in ("robust", "promising", "top50", "all"):
        for x in lab.get(key) or []:
            if isinstance(x, dict) and x.get("id"):
                info.setdefault(x["id"], x)
    fz_start = next_midnight(fz.get("date"))
    lab_day = (lab.get("frozen") or {}).get("date")
    starts = [v for v in (fz_start, next_midnight(lab_day)) if v is not None]
    lab_start = max(starts) if starts else None
    items: list[dict] = []
    seen: set[str] = set()

    def add(x, origen: str, desde: int | None) -> None:
        sid = x.get("id") if isinstance(x, dict) else None
        if not sid or sid in seen or desde is None:
            return
        it = watch_item(sid, desde, x.get("name"), x.get("class") or "prometedora", origen, info.get(sid),
                        lab.get("period"))
        if it is None:
            log.warning("Vigilancia: estrategia %s no válida o fuera de las reglas del usuario: no se vigila",
                        str(sid)[:120])
            return
        seen.add(sid)
        items.append(it)

    for x in (fz.get("robust") or []) + (fz.get("promising") or []):
        add(x, "congelada", fz_start)
    for x in (lab.get("robust") or []) + (lab.get("promising") or []):
        add(x, "laboratorio", lab_start)
    out = []
    for k, it in enumerate(items):
        it["codigo"] = f"E{k + 1}"
        out.append(it)
        vid = it["id"].rsplit("|", 1)[0] + "|" + VARIANT_DUR
        if it["st"]["dur"] == VARIANT_DUR or vid in seen:
            continue
        v = watch_item(vid, it["desde"], None, "variante", "variante", info.get(vid) or VARIANT_REF.get(vid),
                       lab.get("period"), variante_de=it["id"])
        if v is not None:
            seen.add(vid)
            v["codigo"] = f"E{k + 1}-{VARIANT_DUR}"
            out.append(v)
    return out, {"congelada": fz.get("date"), "lista": FROZEN if fz else None, "laboratorio": lab_day}


# ---------------------------------------------------------------- cálculo (funciones puras del laboratorio, en un hilo)

def build_lab(bars: dict[str, np.ndarray], ev_from: int, now: int):
    """Laboratorio de las aperturas desde ev_from (más las anteriores que piden las características) con las velas en
    memoria y las fuentes de la caché, como laboratorio.build_lab. None si no hay aperturas."""
    ops_from = ev_from - PRE_OPEN_D * DAY
    idx = bt.index_data()
    d0 = datetime.fromtimestamp(ops_from / 1000, timezone.utc).date() - timedelta(days=1)
    d1 = datetime.fromtimestamp(now / 1000, timezone.utc).date() + timedelta(days=AHEAD_D)
    ops = [o for o in bt.build_openings(d0, d1, idx)
           if o["id"] in L.WIN_IDS and not L.special_session(o) and o["open_ms"] >= ops_from]
    if not ops:
        return None
    meta = L.opening_meta(ops)
    om = meta["open_ms"]
    feats = L.chain_table(ops, idx)
    feats.update(L.fng_features(om, L.load_fng()))
    feats["gd_z"] = L.gdelt_features(om, L.load_gdelt())
    macro = L.load_macro()
    fund = {c: L.load_funding(c) for c in L.COINS}
    coin_feats = {c: {"fund_pct": L.funding_features(om, fund[c])} for c in L.COINS}
    op = {k: meta[k] for k in ("open_ms", "next_same_ms", "prev_same_ms", "prev_ms", "prev2_ms", "prev_sess")}
    c_from = ops_from - LOOKBACK_D * DAY
    tabs = {}
    for c in L.COINS:
        a = bars.get(c)
        a = a[a[:, 0] >= c_from] if a is not None and len(a) else None
        tabs[c] = L.coin_tables(L.dense_from_rows(a) if a is not None and len(a) else None, op, macro)
    closes = {}
    try:
        closes = {int(r[0]) // DAY: float(r[4]) for r in history.load_klines("BTC", "1d")}
    except (TypeError, ValueError, IndexError):
        log.warning("Vigilancia: velas diarias de BTC ilegibles: se usan las de 1 min")
    for d, v in (tabs["BTC"].get("daily") or {}).items():
        closes.setdefault(d, v)
    feats.update(L.btc_trend(om, closes))
    lab = L.Lab(meta, feats, tabs, int(ev_from), coin_feats)
    lab.funding = {i: fund[c] for i, c in enumerate(lab.coins) if fund.get(c) is not None}
    return lab, ops


def live_trades(lab, st: dict, ev_from: int, blockers: list[tuple[int, int, int]]):
    """Operaciones de una estrategia en las aperturas desde ev_from, como Lab.run pero sin quitar las que siguen
    abiertas (why = 3) y con las ya guardadas de aperturas anteriores ocupando su moneda (una posición por moneda)."""
    if lab.U is None:
        return None
    e, dd = lab.events(st)
    keep = lab.open_ms[e] >= ev_from
    e, dd, u = lab.candidates(st, e[keep], dd[keep])
    if not len(e):
        return None
    res = lab.resolve(u, st["sl"], st["r"], st["dur"])
    ok = res["ok"]
    e, dd = e[ok], dd[ok]
    res = {k: v[ok] for k, v in res.items()}
    if not len(e):
        return None
    coin, ent, ext, prio = lab.coin[e].astype(np.int64), res["ent"], res["ext"], e.astype(np.int64)
    nb = len(blockers)
    if nb:
        b = np.array(blockers, np.int64).reshape(-1, 3)
        coin, ent = np.concatenate((b[:, 0], coin)), np.concatenate((b[:, 1], ent))
        ext, prio = np.concatenate((b[:, 2], ext)), np.concatenate((-1 - np.arange(nb), prio))
    keep = L.no_overlap(coin, ent, ext, prio)[nb:]
    return e[keep], dd[keep], {k: v[keep] for k, v in res.items()}


def _code(sid: str) -> str:
    return hashlib.sha1(sid.encode()).hexdigest()[:10]


def trade_record(lab, ops: list[dict], w: dict, k: int, e: int, d: int, res: dict) -> dict:
    """Fila de la tabla vigilancia. Largos en contado con los costes del laboratorio; cortos en perpetuo (comisión
    FEE_PERP y el funding liquidado entre la entrada y la salida, como laboratorio.short_side)."""
    st = w["st"]
    sl, r = float(st["sl"]), float(st["r"])
    o = ops[e // lab.nc]
    ci = int(lab.coin[e])
    coin = lab.coins[ci]
    E, why, ret = float(res["px"][k]), int(res["why"][k]), float(res["ret"][k])
    ent, ext = int(res["ent"][k]), int(res["ext"][k])
    fee_pct = fund_pct = 0.0
    complete = True
    if d < 0:
        fee_pct = (L.FEE - L.FEE_PERP) * 2 * 100
        f = lab.funding.get(ci)
        if f is not None and len(f):
            a, b = np.searchsorted(f[:, 0], [ent, ext], side="right")
            fund_pct = float(f[a:b, 1].sum()) * 100          # el corto cobra el funding positivo
            step = float(np.median(np.diff(f[-10:, 0]))) if len(f) > 2 else 8.0 * H
            complete = float(f[-1, 0]) + step > ext            # ya está liquidado todo el funding de la operación
        else:
            complete = False
    pct = ret + fee_pct + fund_pct
    closed = why != 3
    key = f"{o['id']}-{o['day']}"
    body = {"apertura_ms": int(o["open_ms"]), "ventana": o["id"], "ventana_nombre": L.WIN_NAME.get(o["id"], o["id"]),
            "dia": o["day"], "instrumento": "perpetuo" if d < 0 else "contado", "sl_pct": sl, "r": r, "dur": st["dur"],
            "R_modelo": round(ret / sl, 4), "pct_modelo": round(ret, 4), "comision_perp_pct": round(fee_pct, 4),
            "funding_pct": round(fund_pct, 5), "funding_completo": bool(complete), "previa": ent < int(w["desde"])}
    if not closed:
        body.update(R_latente=round(pct / sl, 4), pct_latente=round(pct, 4), datos_hasta=ext)
    ratio = (ret / 100 + d + L.FEE) / (d - L.FEE)   # precio de salida con deslizamiento, desde el resultado neto
    return {"id": f"{_code(st['id'])}:{key}:{coin}", "estrategia": st["id"], "apertura": key, "moneda": coin,
            "lado": "largo" if d > 0 else "corto", "entrada_t": ent, "entrada": round(E, 8),
            "stop": round(E * (1 - d * sl / 100), 8), "objetivo": round(E * (1 + d * sl * r / 100), 8),
            "salida_t": ext if closed else None, "salida": round(E * ratio, 8) if closed else None,
            "R": round(pct / sl, 4) if closed else None, "pct": round(pct, 4) if closed else None,
            "motivo": WHY[why], "cuerpo": body}


def evaluate(bars: dict[str, np.ndarray], watch: list[dict], ev_from: int, now: int,
             blockers: dict[str, list[tuple[str, int, int]]]) -> dict:
    """Operaciones virtuales de cada estrategia vigilada en las aperturas desde ev_from, aperturas evaluadas (con sus
    monedas válidas), estrategias calculadas de verdad (calc: con tablas de velas) y hasta dónde llegan las velas de
    cada moneda (hasta). Sin calc o sin la apertura y la moneda evaluadas, una abierta guardada no se anula."""
    out = {"trades": {w["id"]: [] for w in watch}, "openings": [], "calc": [],
           "hasta": {c: int(a[-1, 0]) + MIN for c, a in bars.items() if a is not None and len(a)}}
    built = build_lab(bars, ev_from, now)
    if built is None:
        return out
    lab, ops = built
    if lab.U is None:
        return out
    out["calc"] = [w["id"] for w in watch]
    for i, o in enumerate(ops):
        if o["open_ms"] < ev_from:
            continue
        ok = [c for ci, c in enumerate(lab.coins) if lab.valid[i * lab.nc + ci]]
        if ok:
            out["openings"].append({"key": f"{o['id']}-{o['day']}", "id": o["id"],
                                    "nombre": L.WIN_NAME.get(o["id"], o["id"]), "open_ms": int(o["open_ms"]),
                                    "monedas": ok})
    for w in watch:
        bl = [(lab.coins.index(c), int(a), int(b)) for c, a, b in blockers.get(w["id"], []) if c in lab.coins]
        got = live_trades(lab, w["st"], ev_from, bl)
        if got is None:
            continue
        e, dd, res = got
        out["trades"][w["id"]] = [trade_record(lab, ops, w, k, int(e[k]), int(dd[k]), res) for k in range(len(e))]
    return out


def _merge(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Velas [t, o, h, l, c, v, qv] sin repetir minutos (lo nuevo manda)."""
    if not len(b):
        return a
    if not len(a):
        return b
    if b[0, 0] > a[-1, 0]:
        return np.concatenate((a, b))
    m = np.concatenate((a, b))
    m = m[np.argsort(m[:, 0], kind="stable")]
    return m[np.concatenate((m[1:, 0] != m[:-1, 0], [True]))]


def _ym(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m")


def load_cache(c_from: int, now: int) -> tuple[dict, dict]:
    """Velas de la caché de 1 min desde c_from y, por moneda, desde dónde hay que pedir a Binance (el final de la
    caché o el primer hueco de 15 min o más de las últimas semanas)."""
    bars, cursor = {}, {}
    for c in L.COINS:
        parts = []
        try:
            files = L.month_files(c, _ym(c_from))
        except OSError as e:
            log.warning("Vigilancia: no pude leer la caché de %s: %s", c, e)
            files = []
        for p in files:
            try:
                a = L.parse_zip(p) if p.suffix == ".zip" else L.parse_api(p)
            except Exception as e:   # un archivo dañado no para el resto
                log.warning("Vigilancia: velas ilegibles %s: %s", p, e)
                continue
            if len(a):
                parts.append(a[a[:, 0] >= c_from - DAY])
        a = np.empty((0, 7))
        for x in parts:
            a = _merge(a, x[np.argsort(x[:, 0], kind="stable")])
        bars[c] = a
        if not len(a) or a[0, 0] > c_from + DAY:
            cursor[c] = c_from
            continue
        recent = a[a[:, 0] >= now - HOLE_D * DAY, 0]
        gaps = np.flatnonzero(np.diff(recent) > L.GAP_RUN * MIN) if len(recent) > 1 else []
        cursor[c] = int(recent[gaps[0]]) + MIN if len(gaps) else int(a[-1, 0]) + MIN
    return bars, cursor


def persist(rows_by_coin: dict[str, list[list]], now: int) -> None:
    """Guarda las velas nuevas en la caché (<SIM>-1m-<AAAA-MM>-api.json, como backtest_aperturas._api_month), mezcladas
    con lo que ya hubiera. Los meses con archivo mensual oficial (zip) no se tocan."""
    for coin, rows in rows_by_coin.items():
        sym = bt.SYMS[coin]
        by: dict[str, list] = {}
        for r in rows:
            by.setdefault(_ym(int(r[0])), []).append(r)
        for ym, new in by.items():
            if bt.zip_path(sym, ym).exists():
                continue
            path = bt.part_path(sym, ym)
            old = history._read(path) or {}
            merged = {int(r[0]): r for r in old.get("rows") or []}
            for r in new:
                merged[int(r[0])] = list(r)
            history._write(path, {"symbol": sym, "month": ym, "fetched": now,
                                  "rows": [merged[t] for t in sorted(merged)]})


# ---------------------------------------------------------------- vigilancia en vivo

def _r(x, d: int = 4):
    return None if x is None else round(float(x), d)


def stats(rows: list[dict]) -> dict:
    """Resumen en vivo de una estrategia: cerradas (R medio, ganadoras, por motivo, intervalo aproximado), abiertas y
    las que se quedaron sin datos para resolverlas (no cuentan). R_medio cobra los cortos como perpetuo;
    R_medio_modelo es el mismo cálculo con los costes de contado del laboratorio (el comparable con el backtest)."""
    closed = [r for r in rows if r["salida_t"] is not None and r["R"] is not None]
    n = len(closed)
    Rs = [float(r["R"]) for r in closed]
    out = {"operaciones": n, "abiertas": sum(r["motivo"] == "abierta" for r in rows),
           "sin_datos": sum(r["motivo"] == "sin_datos" for r in rows), "R_medio": _r(statistics.fmean(Rs)) if n else None,
           "R_total": round(sum(Rs), 3), "ganadoras": _r(sum(r["pct"] > 0 for r in closed) / n) if n else None,
           "pct_medio": _r(statistics.fmean(float(r["pct"]) for r in closed)) if n else None,
           "R_medio_modelo": _r(statistics.fmean(float(r["cuerpo"].get("R_modelo", r["R"])) for r in closed))
           if n else None,
           "largos": sum(r["lado"] == "largo" for r in closed), "cortos": sum(r["lado"] == "corto" for r in closed),
           "por_motivo": {m: sum(r["motivo"] == m for r in closed) for m in ("objetivo", "stop", "tiempo")},
           "ic95": None}
    if n >= 5:
        half = 1.96 * statistics.stdev(Rs) / n ** 0.5
        out["ic95"] = [_r(out["R_medio"] - half), _r(out["R_medio"] + half)]
    return out


class Vigilancia:
    """Prueba en avance de las estrategias vigiladas. fetch(coin, start_ms, limit) → velas cerradas de 1 min
    [t, o, h, l, c, v, qv, taker] (por defecto, la API pública de Binance); watch fija la lista (pruebas)."""

    def __init__(self, bus, store, session: aiohttp.ClientSession | None = None, clock=now_ms, fetch=None,
                 watch: list[dict] | None = None, meta: dict | None = None):
        self.bus, self.store, self.session, self.clock = bus, store, session, clock
        self._fetch = fetch or self._fetch_binance
        self._pause = PAGE_PAUSE_S if fetch is None else 0.0
        self._fixed = watch is not None
        self.watch: list[dict] = list(watch or [])
        self.meta = dict(meta or {})
        self._watch_sig = None
        self.bars = {c: np.empty((0, 7)) for c in L.COINS}
        self.cursor: dict[str, int] = {}
        self.unsaved: dict[str, list] = {c: [] for c in L.COINS}
        self._loaded: int | None = None      # desde dónde se cargó la caché
        self._need = True
        self._rounds = 0                     # rondas hechas (antes de la primera, «arrancando»)
        self._watch_err: str | None = None
        self._persist_at = time.monotonic()
        self._funding_at = float("-inf")
        self._fund_task: asyncio.Task | None = None
        self.data_err: dict[str, str] = {}
        self.calc_err: str | None = None
        self.calc_s: float | None = None
        self.msg: dict | None = None
        self._key = None
        self._err_at: dict[str, float] = {}
        db = store.db
        db.executescript(SCHEMA)
        self.rows: dict[str, dict] = {}
        for row in db.execute(f"SELECT {', '.join(COLS)} FROM vigilancia"):
            r = dict(zip(COLS, row))
            try:
                r["cuerpo"] = json.loads(r["cuerpo"] or "{}")
            except ValueError:
                log.error("Vigilancia: fila %s ilegible: se ignora", r["id"])
                continue
            self.rows[r["id"]] = r
        self.state: dict = {}
        for k, v in db.execute("SELECT clave, valor FROM vigilancia_estado"):
            try:
                self.state[k] = json.loads(v)
            except ValueError:
                log.error("Vigilancia: estado «%s» ilegible: se ignora", k)
        if self.rows:
            log.info("Vigilancia: %d operaciones virtuales recuperadas", len(self.rows))

    # ---- base de datos

    def _write(self, r: dict) -> None:
        vals = [r[k] for k in COLS[:-1]] + [json.dumps(r["cuerpo"], ensure_ascii=False)]
        self.store.db.execute(f"INSERT OR REPLACE INTO vigilancia VALUES ({','.join('?' * len(COLS))})", vals)

    def _set_state(self, key: str, value) -> None:
        self.state[key] = value
        self.store.db.execute("INSERT OR REPLACE INTO vigilancia_estado VALUES (?,?)",
                              (key, json.dumps(value, ensure_ascii=False)))

    def _log_err(self, key: str, msg: str, *args, exc: bool = False) -> None:
        """log.warning (con exc, log.exception: la traza completa) con freno: un fallo que se repite cada minuto sale
        una vez cada ERR_LOG_S y no llena el registro."""
        t = time.monotonic()
        if t - self._err_at.get(key, float("-inf")) >= ERR_LOG_S:
            self._err_at[key] = t
            (log.exception if exc else log.warning)(msg, *args)

    # ---- estrategias

    async def _load_watch(self) -> list[dict]:
        if self._fixed:
            return self.watch
        sig = []
        for name in (FROZEN, LAB_FILE):
            try:
                sig.append((config.BACKTEST_DIR / name).stat().st_mtime)
            except OSError:
                sig.append(None)
        if sig != self._watch_sig:
            try:
                self.watch, self.meta = await asyncio.to_thread(load_watch)
                self._watch_sig, self._watch_err = sig, None
                self._need = True
                log.info("Vigilancia: %d estrategias vigiladas (%s)", len(self.watch),
                         ", ".join(f"{w['codigo']} {w['clase']}" for w in self.watch) or "ninguna")
            except Exception as e:
                self._watch_err = str(e)[:160] or type(e).__name__
                self._log_err("watch", "Vigilancia: no pude leer la lista de estrategias: %s", e)
        return self.watch

    # ---- velas

    async def _fetch_binance(self, coin: str, start: int, limit: int) -> list[list]:
        if self.session is None:
            return []
        params = {"symbol": apertura.PAIRS[coin], "interval": "1m", "startTime": str(start), "limit": str(limit)}
        async with self.session.get(apertura.KLINES, params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
            r.raise_for_status()
            # Con margen: si el reloj del PC adelanta, la vela en curso no pasa por cerrada.
            return apertura.to_bars(await r.json(content_type=None), self.clock() - SETTLE_MS)

    def _changed(self, c: str, rows: list[list], seen_to: int) -> int:
        """Velas nuevas (desde seen_to) o que cambian respecto a las que ya había en memoria (las repedidas)."""
        a = self.bars[c]
        n = 0
        for r in rows:
            t = int(r[0])
            if t >= seen_to or not len(a):
                n += 1
                continue
            i = int(np.searchsorted(a[:, 0], t))
            n += i >= len(a) or a[i, 0] != t or not np.array_equal(a[i, 1:7], np.asarray(r[1:7], float))
        return n

    async def _refresh(self, c_from: int, now: int) -> int:
        """Pide a Binance las velas cerradas que faltan de cada moneda y vuelve a pedir los últimos REFETCH_MIN minutos
        (lo nuevo manda: una vela tomada a medias se corrige en la ronda siguiente). Devuelve cuántas son nuevas o han
        cambiado. Un fallo en una moneda no para las demás."""
        got = 0
        for c in L.COINS:
            a = self.bars[c]
            if len(a) and a[0, 0] < c_from - DAY:
                self.bars[c] = a[a[:, 0] >= c_from - DAY]
            seen_to = max(int(self.cursor.get(c) or c_from), c_from)
            cur = max(seen_to - REFETCH_MIN * MIN, c_from) if len(self.bars[c]) else seen_to
            pages = 0
            try:
                while cur + MIN <= now and pages < MAX_PAGES:
                    raw = await self._fetch(c, cur, PAGE) or []
                    pages += 1
                    rows = [list(r) for r in raw if int(r[0]) >= cur and int(r[0]) + MIN <= now]
                    if not rows:
                        break
                    got += self._changed(c, rows, seen_to)
                    self.bars[c] = _merge(self.bars[c], np.array([r[:7] for r in rows], float))
                    self.unsaved[c].extend(rows)
                    cur = int(rows[-1][0]) + MIN
                    if len(raw) < PAGE:
                        break
                    if self._pause:
                        await asyncio.sleep(self._pause)
                self.data_err.pop(c, None)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.data_err[c] = str(e)[:160] or type(e).__name__
                self._log_err("fetch:" + c, "Vigilancia: velas de %s no disponibles: %s", c, e)
            self.cursor[c] = max(cur, seen_to)   # un fallo al repedir no hace retroceder el cursor
        return got

    async def _persist(self, force: bool = False) -> None:
        """Velas nuevas a la caché (la comparten el laboratorio y los backtests): solo las cerradas hace más de
        REFETCH_MIN minutos, ya repedidas al menos una vez; las últimas esperan a la próxima (al cerrar se pierden y se
        vuelven a pedir al arrancar, porque el cursor sale del final de la caché)."""
        if not force and time.monotonic() - self._persist_at < PERSIST_S:
            return
        self._persist_at = time.monotonic()
        cut = self.clock() - REFETCH_MIN * MIN
        todo, wait = {}, {}
        for c, rows in self.unsaved.items():
            old = [r for r in rows if int(r[0]) + MIN <= cut]
            if old:
                todo[c] = old
            wait[c] = [r for r in rows if int(r[0]) + MIN > cut]
        self.unsaved = {c: wait.get(c, []) for c in L.COINS}
        if not todo:
            return
        try:
            await asyncio.to_thread(persist, todo, self.clock())
        except Exception as e:   # se reintenta en la próxima: las velas siguen en memoria
            for c, rows in todo.items():
                self.unsaved[c] = rows + self.unsaved[c]
            self._log_err("persist", "Vigilancia: no pude guardar las velas en la caché: %s", e)

    def _funding_due(self) -> None:
        """Lanza la descarga del funding (cada 4 h) en una tarea aparte: la ronda de velas no la espera."""
        if self.session is None or (self._fund_task is not None and not self._fund_task.done()) \
                or time.monotonic() - self._funding_at < FUNDING_S:
            return
        self._funding_at = time.monotonic()
        self._fund_task = asyncio.create_task(self._funding())

    async def _funding(self) -> None:
        """Funding de los perpetuos (lo usan los cortos). Sin red o si falla, se usa lo que haya."""
        for sym in bt.SYMS.values():
            try:
                await asyncio.wait_for(historia_extra.fetch_funding(self.session, sym), FUNDING_WAIT_S)
                self._need = True
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log_err("funding:" + sym, "Vigilancia: funding de %s no disponible: %s", sym, e)

    # ---- ronda

    def _ev_from(self, now: int, start: int, watch: list[dict]) -> int:
        """Primera apertura que se recalcula: las de los últimos KEEP_D días, desde la última ronda si el bot estuvo
        apagado, desde la apertura más antigua con una operación abierta y desde el calentamiento si hay estrategias
        nuevas en la lista."""
        lo = max(start - WARM_D * DAY, now - MAX_BACK_D * DAY)
        last = self.state.get("ultimo_calculo")
        ids = {w["id"] for w in watch}
        upto = self.state.get("datos_hasta")   # hasta donde había velas en la última ronda
        if not last or not upto or ids - set(self.state.get("vigiladas") or []):
            return lo
        ev = min(now, int(last), int(upto)) - KEEP_D * DAY
        opens = [r["cuerpo"].get("apertura_ms") or 0 for r in self.rows.values()
                 if r["motivo"] == "abierta" and r["estrategia"] in ids]
        if opens:
            ev = min(ev, min(opens))
        return max(lo, ev)

    def _expire(self, ev_from: int, ids: set[str]) -> None:
        """Abiertas de aperturas anteriores a ev_from: ya no se recalculan (solo pasa tras un apagón de más de
        MAX_BACK_D días). Quedan «sin_datos»: no se resuelven, no cuentan y no ocupan su moneda."""
        n = 0
        for rid, r in list(self.rows.items()):
            if r["estrategia"] in ids and r["motivo"] == "abierta" and (r["cuerpo"].get("apertura_ms") or 0) < ev_from:
                r = {**r, "motivo": "sin_datos"}
                self._write(r)
                self.rows[rid] = r
                n += 1
                log.warning("Vigilancia: operación virtual %s sin datos para resolverla (apagón largo): no cuenta", rid)
        if n:
            self.store.db.commit()

    def _blockers(self, ev_from: int) -> dict[str, list[tuple[str, int, int]]]:
        """Operaciones guardadas de aperturas anteriores a ev_from que aún ocupan su moneda en ese momento (las
        «sin_datos» no ocupan nada)."""
        out: dict[str, list] = {}
        for r in self.rows.values():
            if (r["cuerpo"].get("apertura_ms") or 0) >= ev_from or r["motivo"] == "sin_datos":
                continue
            ext = r["salida_t"] if r["salida_t"] is not None else BIG
            if ext > ev_from:
                out.setdefault(r["estrategia"], []).append((r["moneda"], int(r["entrada_t"]), int(ext)))
        return out

    def _save(self, res: dict, ev_from: int, now: int, watch: list[dict]) -> None:
        """Inserta o actualiza cada operación virtual (una cerrada ya no cambia) y quita las abiertas que el cálculo
        ya no da (p. ej., un hueco de datos de 15 min o más que la anula, como en el laboratorio), pero solo si su
        apertura y su moneda se han recalculado de verdad, con velas hasta donde llegaba la operación: un cálculo sin
        velas (reinicio sin caché y con Binance caído, una moneda sin datos) no anula nada."""
        seen = set()
        codes = {w["id"]: w.get("codigo", "") for w in watch}
        for sid, recs in res["trades"].items():
            for r in recs:
                seen.add(r["id"])
                old = self.rows.get(r["id"])
                if old is not None and old["salida_t"] is not None and old["cuerpo"].get("funding_completo", True):
                    continue
                if old == r:
                    continue
                self._write(r)
                self.rows[r["id"]] = r
                if r["cuerpo"].get("previa"):
                    continue
                if old is None:
                    log.info("Vigilancia: %s entra (virtual) %s %s en %s a %s", codes.get(sid), r["lado"], r["moneda"],
                             r["apertura"], r["entrada"])
                if r["salida_t"] is not None and (old is None or old["salida_t"] is None):
                    log.info("Vigilancia: %s %s %s en %s sale por %s: %+.2f R", codes.get(sid), r["lado"], r["moneda"],
                             r["apertura"], r["motivo"], r["R"])
        ids = set(res["trades"])
        calc = set(res.get("calc") or ())
        evald = {o["key"]: set(o.get("monedas") or ()) for o in res["openings"]}
        hasta = res.get("hasta") or {}
        for rid, r in list(self.rows.items()):
            b = r["cuerpo"]
            if (r["estrategia"] in calc and r["motivo"] == "abierta" and rid not in seen
                    and (b.get("apertura_ms") or 0) >= ev_from and r["moneda"] in evald.get(r["apertura"], ())
                    and hasta.get(r["moneda"], 0) >= (b.get("datos_hasta") or 0)):
                log.info("Vigilancia: operación virtual %s anulada (el cálculo ya no la da)", rid)
                self.store.db.execute("DELETE FROM vigilancia WHERE id=?", (rid,))
                del self.rows[rid]
        ops = {o["key"]: o for o in self.state.get("aperturas") or [] if isinstance(o, dict) and o.get("key")}
        for o in res["openings"]:
            ops[o["key"]] = o
        keep = sorted(ops.values(), key=lambda o: o["open_ms"])[-KEEP_OPENINGS:]
        if keep != self.state.get("aperturas"):   # hasta 400 aperturas: solo si cambian
            self._set_state("aperturas", keep)
        self._set_state("ultimo_calculo", int(now))
        data_to = self._data_to()
        if data_to is not None:
            self._set_state("datos_hasta", int(min(data_to, now)))
        if sorted(ids) != self.state.get("vigiladas"):
            self._set_state("vigiladas", sorted(ids))
        self.store.db.commit()

    async def step(self, now: int | None = None) -> None:
        """Una ronda: velas nuevas, cálculo en un hilo si hay algo nuevo, guardado y estado del bus."""
        now = self.clock() if now is None else int(now)
        watch = await self._load_watch()
        if watch:
            start = min(w["desde"] for w in watch)
            ev_from = self._ev_from(now, start, watch)
            c_from = ev_from - (PRE_OPEN_D + LOOKBACK_D) * DAY
            new = 0
            try:
                if self._loaded is None or c_from < self._loaded - DAY:   # al arrancar o si hay que mirar más atrás
                    bars, cursor = await asyncio.to_thread(load_cache, c_from, now)
                    for c in L.COINS:
                        mem = self.bars[c]
                        cc = cursor.get(c, c_from)   # lo que ya está en memoria no se vuelve a pedir
                        self.cursor[c] = cc if not len(mem) or cc < mem[0, 0] else self.cursor.get(c, cc)
                        self.bars[c] = _merge(bars.get(c, np.empty((0, 7))), mem)
                    self._loaded = c_from
                new = await self._refresh(c_from, now)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log_err("datos", "Vigilancia: fallo al preparar las velas: %s", e)
            await self._persist()
            self._funding_due()
            # Sin velas de las 4 monedas no se calcula (tras un reinicio sin caché y con Binance caído, el cálculo no
            # daría nada y no hay que tocar lo guardado): se calcula en cuanto vuelvan.
            if (new or self._need) and self._data_to() is not None:
                self._expire(ev_from, {w["id"] for w in watch})
                t0 = time.monotonic()
                try:
                    res = await asyncio.to_thread(evaluate, dict(self.bars), watch, ev_from, now,
                                                  self._blockers(ev_from))
                    self._save(res, ev_from, now, watch)
                    self.calc_err, self._need = None, False
                    self.calc_s = round(time.monotonic() - t0, 2)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.calc_err, self._need = str(e)[:200] or type(e).__name__, True   # se repite aunque no haya velas
                    self._log_err("calculo", "Vigilancia: fallo en el cálculo; se reintenta en la próxima ronda",
                                  exc=True)
        self._rounds += 1
        self._publish(now, watch)

    async def run(self) -> None:
        if not ENABLED:
            log.info("Vigilancia apagada (BOT_VIGILANCIA=0)")
            self._publish(self.clock(), [])
            return
        self._publish(self.clock(), self.watch)   # «arrancando» mientras se cargan la lista y las velas
        while True:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._log_err("ronda", "Vigilancia: fallo en la ronda; sigue en la próxima", exc=True)
            await asyncio.sleep(TICK_S)

    async def close(self) -> None:
        t = self._fund_task
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await self._persist(force=True)

    # ---- estado

    def _data_to(self) -> int | None:
        ends = [int(a[-1, 0]) + MIN for a in self.bars.values() if len(a)]
        return min(ends) if len(ends) == len(L.COINS) else None

    def _next_eval(self, now: int, start: int | None) -> dict | None:
        """Primera apertura aún sin evaluar después de la última evaluada (con datos atrasados, su hora ya pasó)."""
        evs = [o for o in self.state.get("aperturas") or [] if isinstance(o, dict)]
        last = max((o.get("open_ms") or 0 for o in evs), default=0)
        floor = max(last + 1, start or 0, now - 2 * DAY)
        d = datetime.fromtimestamp(floor / 1000, timezone.utc).date()
        for k in range(-1, 8):
            for o in apertura.openings_on(d + timedelta(days=k)):
                if o["id"] in L.WIN_IDS and o["open_ms"] >= floor:
                    return {"key": f"{o['id']}-{o['day']}", "nombre": L.WIN_NAME.get(o["id"], o["id"]),
                            "open_ms": int(o["open_ms"]), "evalua_ms": int(o["open_ms"] + EVAL_MIN * MIN)}
        return None

    def build_status(self, now: int, watch: list[dict] | None = None) -> dict:
        watch = self.watch if watch is None else watch
        start = min((w["desde"] for w in watch), default=None)
        data_to = self._data_to()
        items, by_open = [], {}
        for w in watch:
            rows = [r for r in self.rows.values() if r["estrategia"] == w["id"] and not r["cuerpo"].get("previa")]
            for r in rows:
                by_open.setdefault(r["apertura"], []).append(
                    {"estrategia": w["id"], "codigo": w.get("codigo"), "moneda": r["moneda"], "lado": r["lado"],
                     "motivo": r["motivo"], "R": r["R"]})
            open_ = sorted((r for r in rows if r["motivo"] == "abierta"), key=lambda r: r["entrada_t"])
            closed = sorted((r for r in rows if r["salida_t"] is not None), key=lambda r: r["salida_t"])
            items.append({
                "id": w["id"], "codigo": w.get("codigo"), "nombre": w["nombre"], "clase": w["clase"],
                "origen": w["origen"], "variante_de": w.get("variante_de"), "en_paper": bool(w.get("en_paper")),
                "desde": w["desde"], "reglas": w["reglas"], "vivo": stats(rows), "backtest": w.get("bt"),
                "abiertas": [{"moneda": r["moneda"], "lado": r["lado"], "apertura": r["apertura"],
                              "ventana": r["cuerpo"].get("ventana_nombre"), "entrada_t": r["entrada_t"],
                              "entrada": r["entrada"], "stop": r["stop"], "objetivo": r["objetivo"],
                              "R_latente": r["cuerpo"].get("R_latente")} for r in open_],
                "ultimas": [{"moneda": r["moneda"], "lado": r["lado"], "apertura": r["apertura"],
                             "ventana": r["cuerpo"].get("ventana_nombre"), "entrada_t": r["entrada_t"],
                             "salida_t": r["salida_t"], "R": r["R"], "pct": r["pct"], "motivo": r["motivo"]}
                            for r in reversed(closed[-6:])]})
        evals = [o for o in self.state.get("aperturas") or [] if isinstance(o, dict) and start is not None
                 and (o.get("open_ms") or 0) >= start]
        bad = ", ".join(f"{c}: {e}" for c, e in sorted(self.data_err.items()))
        if not ENABLED:
            estado, nota = "apagada", "La vigilancia está apagada (BOT_VIGILANCIA=0)."
        elif not watch and not self._fixed and self._watch_sig is None and self._watch_err is None:
            estado, nota = "arrancando", "Leyendo la lista de estrategias vigiladas…"
        elif not watch:
            estado, nota = "sin_estrategias", (
                f"No pude leer la lista de estrategias ({self._watch_err}): se reintenta en la próxima ronda."
                if self._watch_err else
                "No hay lista congelada ni estrategias prometedoras en laboratorio.json: no hay nada que vigilar.")
        elif self.calc_err:
            estado, nota = "error", f"El último cálculo falló ({self.calc_err}); se reintenta en la próxima ronda."
        elif not self._rounds:
            estado, nota = "arrancando", "Cargando las velas de 1 min y calculando las aperturas…"
        elif data_to is None:
            estado, nota = "sin_datos", ("Faltan velas de Binance" + (f" ({bad})" if bad else "")
                                         + ": no se calcula nada ni se toca lo guardado hasta que vuelvan.")
        elif not self.state.get("ultimo_calculo"):
            estado, nota = "arrancando", "Cargando las velas de 1 min y calculando las aperturas…"
        elif now - data_to > STALE_MS:
            estado, nota = "datos_atrasados", ("Las velas de Binance no llegan al día" + (f" ({bad})" if bad else "")
                                               + ": las aperturas se evaluarán cuando vuelvan.")
        else:
            estado, nota = "ok", None
        return {"type": "vigilancia", "ts": now, "estado": estado, "nota": nota, "aviso": AVISO, "costes": COSTES,
                "desde": start, "desde_dia": datetime.fromtimestamp(start / 1000, MAD).date().isoformat()
                if start is not None else None,
                "congelada": self.meta.get("congelada"), "lista": self.meta.get("lista"),
                "datos_hasta": data_to, "ultimo_calculo": self.state.get("ultimo_calculo"), "calculo_s": self.calc_s,
                "evaluacion": f"Cada apertura se evalúa unas {EVAL_MIN // 60} h después (el laboratorio usa "
                              f"{L.POST_BARS // 60} h de velas tras la apertura); cada operación lleva su hora real "
                              "de entrada y salida.",
                "aperturas_evaluadas": len(evals),
                "aperturas": [{**o, "entradas": by_open.get(o["key"], [])} for o in reversed(evals[-8:])],
                "proxima": self._next_eval(now, start) if watch else None, "estrategias": items}

    def _publish(self, now: int, watch: list[dict]) -> None:
        msg = self.build_status(now, watch)
        key = json.dumps({k: v for k, v in msg.items() if k != "ts"}, sort_keys=True, ensure_ascii=False)
        self.msg = msg
        if key != self._key:
            self._key = key
            self.bus.publish(msg)

    def view(self, limit: int = 500) -> dict:
        """GET /api/vigilancia: el último estado y las operaciones virtuales (las que cuentan), de la más reciente a la
        más antigua."""
        msg = dict(self.msg or self.build_status(self.clock()))
        ids = {w["id"] for w in self.watch}
        rows = sorted((r for r in self.rows.values() if r["estrategia"] in ids and not r["cuerpo"].get("previa")),
                      key=lambda r: -r["entrada_t"])
        msg["operaciones"] = rows[:limit]
        return msg
