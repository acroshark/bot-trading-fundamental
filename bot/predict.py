"""Predicciones (contrato 2.4): probabilidad de que cada cripto suba en la próxima ventana y en la próxima hora y, por
compatibilidad (OBSOLETAS desde el 28-10-2026: la web ya no las enseña), de que cada bolsa cierre su próxima sesión por
encima de la apertura. No son órdenes.

Cada predicción parte de una tasa base y la ajusta, sumando en log-odds, con factores visibles. Las de cripto ya no usan
los cierres de los índices de bolsa (al usuario le importa la cripto, no el Nikkei): tasa base de la cripto en esa
apertura (bot/reaccion.py, velas de 1 min, o el backtest), la reacción de la cripto en la apertura anterior medida sin
mirar el futuro (si no predice, pesa 0 y se dice), su tendencia de 4 h, el flujo de órdenes, el minorista extremo y sus
noticias. Las noticias pesan por su media ponderada por credibilidad (no se saturan con el volumen), con el tono medio
de las últimas 24 h descontado, y las macro o geopolíticas solo si la cripto las reflejó (bot/reflejo.py).
Se publican cada 5 min, se guardan en la tabla predictions y, cuando pasa su horizonte, se comprueban con los datos de
tendencia y mercado. El acierto lleva al lado la proporción de veces que subió (up_rate): «siempre baja» acertaría
1 − up_rate, y sin superar eso no hay mérito."""
import asyncio
import json
import logging
import math
import statistics
import time
from collections import deque
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import apertura, backtest, config, reflejo, regions
from .market import fresh_flow
from .backtest import ASIA, VERDICT_TXT, binom_sf, known, prev_weekday
from .local_ai import WINDOWS, upcoming_windows

log = logging.getLogger("predict")

H = 3600_000
M = regions.MARKETS
ROUND_S = 300                # una ronda de predicciones cada 5 min
TICK_S = 15                  # precios de referencia y evaluación
START_DELAY_S = 60           # deja llegar antes la primera tendencia y los precios
LOOKAHEAD_MS = 24 * H        # bolsas que abren en las próximas 24 h
P_MIN, P_MAX = 0.05, 0.95
MIN_PP = 0.05                # factores más pequeños no se muestran
N_HALF = 150                 # con 150 casos la fiabilidad por tamaño vale 0,5
P_CUT = 0.10                 # con este p-valor o más la señal no pesa
# no_funciona pesa 0. «solo_correlacion» es una relación firme que no se puede operar (de cierre a cierre o sin la
# información a tiempo): para predecir de cierre a cierre vale tanto como «funciona».
VERDICT_W = {"funciona": 1.0, "solo_correlacion": 1.0, "dudosa": 0.6, "pocos_datos": 0.5}
LAG1 = "nuevayork"           # bolsa cuya sesión anterior sirve a las que abren antes de que ella cierre
NEWS_CAP_PP = 10.0           # tope del factor de noticias (lo alcanza solo un flujo unánime de noticias extremas)
NEWS_K = 1.0                 # confianza Σcred / (Σcred + K): una noticia de credibilidad 100 vale la mitad
NEWS_BOLSA_H = 12
NEWS_CRIPTO_H = 6
NEWS_BIAS_H = 24             # tono medio de las noticias de 24 h, que se descuenta (el triaje sale casi siempre bajista)
NEWS_BIAS_MIN = 20           # con menos noticias con dirección no se descuenta nada
CHAIN_LIFT_PP = 2.0          # la reacción de la apertura anterior pesa solo si mueve ≥ 2 puntos y es significativa
CHAIN_P = 0.05
REACT_H = 4                  # la reacción de la cripto a una apertura se mide a 4 h (bot/reaccion.py)
WIN_NAMES = {**{g[0]: g[1] for g in apertura.GROUPS.values()}, "saopaulo": "São Paulo", "nuevayork": "Nueva York"}
FUT_PP_PER_PCT = 5.0         # +1 % en el futuro = +5 puntos (escala sin validar en el backtest)
FUT_CAP_PP = 5.0
FUT_STALE_MS = 15 * 60_000   # futuro sin actualizar en 15 min: no se usa
OF_STALE_MS = 60_000         # flujo de órdenes de hace más de 1 min: no se usa
PX_STALE_MS = 2 * 60_000     # precios de cripto sin mensaje nuevo en 2 min: el WebSocket se ha caído
FLOW_CAP_PP = 6.0
RETAIL_PP = 4.0
WINDOW_H = 4                 # horizonte de las ventanas cripto (el de bolsa_a_cripto)
FLOW_FULL_H, FLOW_ZERO_H = 1, 6   # el flujo de ahora pesa entero si la ventana empieza en 1 h y nada a partir de 6 h
CLOSE_GRACE_MS = 20 * 60_000      # Yahoo da algunos índices con 15-20 min de retraso
LATE_MS = 15 * 60_000             # margen para tomar el precio de cripto al empezar y al acabar
EXPIRE_MS = 3 * 86400_000         # bolsa sin datos de su sesión tras esto: no se evalúa (festivo o bot apagado)
PRUNE_MS = 14 * 86400_000         # las rondas intermedias se borran a los 14 días; la última antes del evento se queda
PX_KEEP = 5 * 3600 // TICK_S      # 5 h de precios propios por moneda
TYPICAL_1H_PCT = {"BTC": 0.45, "ETH": 0.6, "SOL": 0.8, "XRP": 0.75}   # sin historial propio de precios
TREND_SIGN = {"alcista": 1, "bajista": -1, "lateral": 0}
OPEN_CLOSE, CLOSE_CLOSE = "apertura_a_cierre", "cierre_a_cierre"   # cómo se mide la sesión de una bolsa
MEASURE_TXT = {OPEN_CLOSE: "de apertura a cierre", CLOSE_CLOSE: "de cierre a cierre"}
WINDOW_KEY = {"Apertura Tokio": "tokio", "Apertura Londres": "londres", "Datos macro EE.UU.": "macro",
              "Apertura Nueva York": "nuevayork", "Apertura CME (domingo)": "cme"}
WINDOW_TZ = {w[0]: w[1] for w in WINDOWS}
TZ_CITY = {"Asia/Tokyo": "Tokio", "Europe/London": "Londres", "America/New_York": "Nueva York"}
# La predicción que cuenta para el acierto es la última ronda de cada id antes de su evento. Al arrancar se agrega
# en SQLite por (kind, horizonte): evaluadas, aciertos y suma de Brier.
ACC_SQL = ("SELECT p.kind, p.horizon, COUNT(*), SUM(p.hit), SUM((p.prob_up - (p.outcome_pct > 0)) * "
           "(p.prob_up - (p.outcome_pct > 0))), SUM(p.outcome_pct > 0) FROM predictions p JOIN "
           "(SELECT id, MAX(ts) AS ts FROM predictions WHERE ts <= when_utc GROUP BY id) f "
           "ON p.id = f.id AND p.ts = f.ts WHERE p.hit >= 0 GROUP BY p.kind, p.horizon")
FINAL_ONE_SQL = ("SELECT kind, horizon, prob_up, outcome_pct, hit FROM predictions WHERE id = ? AND ts <= when_utc "
                 "ORDER BY ts DESC LIMIT 1")
# Recorre por when_utc (índice propio) de la más reciente hacia atrás y para al llegar a n.
EVALUATED_SQL = ("SELECT p.outcome_pct, p.hit, p.body FROM predictions p "
                 "WHERE p.hit IS NOT NULL AND p.ts = (SELECT MAX(q.ts) FROM predictions q WHERE q.id = p.id "
                 "AND q.ts <= q.when_utc) ORDER BY p.when_utc DESC LIMIT ?")


# ---------- Textos ----------
def _num(x: float, d: int = 1) -> str:
    return f"{x:.{d}f}".replace(".", ",")


def _signed(x: float, d: int = 1) -> str:
    return ("+" if x >= 0 else "") + _num(x, d)


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " y " + names[-1]


def _short(s: str, n: int = 90) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1].rsplit(" ", 1)[0] + "…"


# ---------- Cálculo ----------
def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _logit(p: float) -> float:
    p = _clip(p, 1e-4, 1 - 1e-4)
    return math.log(p / (1 - p))


def reliability(s: dict | None) -> float:
    """Peso de 0 a 1 de una estrategia del backtest según su n, su p-valor y su veredicto. Vale 0 si la información no
    se conoce a tiempo (actionable) o si el backtest dice que no funciona. Una «dudosa» (falla fuera de muestra o en
    la corrección por comparaciones múltiples) pesa menos que una «solo_correlacion» firme."""
    if not s or not s.get("actionable", True) or s.get("verdict") not in VERDICT_W:
        return 0.0
    n, p = float(s.get("n") or 0), float(s.get("p_value", 1.0))
    return n / (n + N_HALF) * max(0.0, 1 - p / P_CUT) * VERDICT_W[s["verdict"]]


def edge_pp(s: dict, up: bool) -> tuple[float, float]:
    """(p condicionada − base) en puntos, ponderado por la fiabilidad. Una estrategia de venta acierta cuando baja,
    así que su ventaja resta a la probabilidad de subida."""
    w = reliability(s)
    return (1 if up else -1) * (float(s.get("hit_rate") or 0) - float(s.get("base_rate") or 0)) * 100 * w, w


def combine(base: float, drivers: list[dict]) -> tuple[float, list[dict]]:
    """Suma en log-odds. Cada factor trae raw_pp (lo que movería él solo la tasa base). Los de un mismo grupo (bolsas que
    cierran casi a la vez y se parecen) se encogen con 1/√k para no contar k veces lo mismo. effect_pp reparte el cambio
    final entre los factores, así que la suma es exactamente prob_up − base."""
    base = _clip(base, P_MIN, P_MAX)
    lb = _logit(base)
    groups: dict[str, int] = {}
    for d in drivers:
        if d.get("group"):
            groups[d["group"]] = groups.get(d["group"], 0) + 1
    deltas = [(_logit(_clip(base + d["raw_pp"] / 100, 0.001, 0.999)) - lb) / math.sqrt(groups.get(d.get("group"), 1))
              for d in drivers]
    total = sum(deltas)
    prob = _clip(1 / (1 + math.exp(-(lb + total))), P_MIN, P_MAX)
    slope = (prob - base) / total if abs(total) > 1e-9 else base * (1 - base)
    out = []
    for d, delta in zip(drivers, deltas):
        e = {k: v for k, v in d.items() if k not in ("raw_pp", "group")}
        e["effect_pp"] = round(delta * slope * 100, 2)
        out.append(e)
    out.sort(key=lambda d: -abs(d["effect_pp"]))
    return prob, out


def confidence(n: int, effects: list[float], prob: float) -> str:
    """Alta con mucha historia, varios factores de acuerdo y una probabilidad lejos del 50 %."""
    total = sum(abs(e) for e in effects)
    agree = abs(sum(effects)) / total if total else 0.0
    edge = abs(prob - 0.5)
    if n >= 500 and len(effects) >= 2 and agree >= 0.7 and edge >= 0.08:
        return "alta"
    if (n >= 100 or len(effects) >= 2) and agree >= 0.5 and edge >= 0.05:
        return "media"
    return "baja"


def news_points(tr: dict) -> float | None:
    """Dirección · magnitud · credibilidad / 1e4 del triaje v2 (contrato 2.7); None si no trae repercusión."""
    rep = tr.get("repercusion")
    if not isinstance(rep, dict) or rep.get("direccion") is None:
        return None
    return float(rep["direccion"]) * float(rep.get("magnitud") or 0) * float(tr.get("credibilidad", 50)) / 1e4


def news_signal(tr: dict) -> tuple[float, float] | None:
    """(tono de −1 a 1 = dirección/100 · magnitud/100, peso = credibilidad/100) de una triada; None sin dirección."""
    rep = tr.get("repercusion")
    if not isinstance(rep, dict) or not rep.get("direccion"):
        return None
    s = float(rep["direccion"]) / 100 * float(rep.get("magnitud") or 0) / 100
    w = max(0.0, min(1.0, float(tr.get("credibilidad", 50)) / 100))
    return (s, w) if s and w else None


def news_bias(news: list[tuple[int, dict]], since: int) -> float:
    """Tono medio (ponderado por credibilidad) de todas las triadas con dirección desde `since`. El triaje sale bajista
    casi siempre (72 h del 28-10-2026: 1.697 bajistas y 313 alcistas): sin descontarlo, toda predicción baja. Con menos
    de NEWS_BIAS_MIN noticias, 0."""
    ws, sw, n = 0.0, 0.0, 0
    for ts, tr in news:
        sig = news_signal(tr) if ts >= since else None
        if sig:
            ws += sig[0] * sig[1]
            sw += sig[1]
            n += 1
    return ws / sw if n >= NEWS_BIAS_MIN and sw > 0 else 0.0


def news_pressure(news: list[tuple[int, dict]], match, since: int, bias: float = 0.0) -> tuple[float, int, tuple | None]:
    """Noticias triadas desde `since` que cumplen match(repercusion): su tono medio ponderado por credibilidad, menos el
    tono medio del día (bias), por la confianza Σcred / (Σcred + NEWS_K), en puntos (tope ±NEWS_CAP_PP). Una media y no
    una suma: cien noticias bajistas no valen cien veces una (antes una sola ya llegaba al tope de −10 y todas las
    predicciones salían bajistas, hallazgo n.º 3 de docs/auditoria-2026-10-28.md).
    Devuelve (puntos, cuántas, (peso, resumen) de la de más peso)."""
    ws, sw, n, top = 0.0, 0.0, 0, None
    for ts, tr in news:
        rep = tr.get("repercusion")
        if ts < since or not isinstance(rep, dict) or not match(rep):
            continue
        sig = news_signal(tr)
        if not sig:
            continue
        s, w = sig
        ws += s * w
        sw += w
        n += 1
        if top is None or abs(s * w) > abs(top[0]):
            top = (s * w, tr.get("resumen") or "")
    if not n:
        return 0.0, 0, None
    raw = NEWS_CAP_PP * sw / (sw + NEWS_K) * (ws / sw - bias)
    return _clip(raw, -NEWS_CAP_PP, NEWS_CAP_PP), n, top


def flow_effect(of: dict | None, price: float | None = None) -> tuple[float, str] | None:
    """Flujo de órdenes → hasta ±6 puntos: media del desequilibrio del libro (±0,5 ya es fuerte), del CVD de 15 min
    frente al volumen de esos 15 min (un 33 % neto ya es fuerte) y de la tendencia."""
    if not of:
        return None
    parts, bits = [], []
    imb, cvd, vol = of.get("imbalance"), of.get("cvd_15m"), of.get("vol_5m_usd")
    px = of.get("price") or price
    tr = of.get("trend")
    if tr in TREND_SIGN:
        parts.append(TREND_SIGN[tr])
        bits.append(f"tendencia {tr}")
    if imb is not None:
        parts.append(_clip(imb / 0.5, -1, 1))
        bits.append(f"desequilibrio {_signed(imb, 2)}")
    if cvd is not None and vol and px:
        parts.append(_clip(cvd / (3 * vol / px) * 3, -1, 1))
        bits.append(f"CVD 15 min {_signed(cvd, 1)}")
    if not parts:
        return None
    return FLOW_CAP_PP * sum(parts) / len(parts), "Flujo de órdenes: " + ", ".join(bits)


def _bias_txt(ctx: dict) -> str:
    b = ctx.get("news_bias") or 0.0
    return f"; descontado el tono medio de las noticias de {NEWS_BIAS_H} h ({_signed(b, 2)})" if b else ""


class BacktestIndex:
    """Lo que las predicciones necesitan de resultados.json (contrato 3.1), indexado una vez por cada backtest nuevo."""

    def __init__(self, res: dict | None):
        res = res or {}
        self.generated = res.get("generated")
        self.by_id = {s["id"]: s for s in res.get("strategies") or [] if isinstance(s, dict) and "id" in s}
        self.ll = res.get("leadlag") or {}
        self.pos = {k: i for i, k in enumerate(self.ll.get("markets") or [])}
        self._mbase: dict[str, tuple | None] = {}

    def get(self, sid: str) -> dict | None:
        return self.by_id.get(sid)

    def diag(self, field: str, b: str):
        j = self.pos.get(b)
        try:
            return self.ll[field][j][j]
        except (KeyError, IndexError, TypeError):
            return None

    def market_base(self, b: str) -> tuple[float, int, str] | None:
        """(tasa base, n, medida) de la sesión de B: de apertura a cierre (mediana de sus estrategias operables de una
        pata) o, si el índice no tiene apertura fiable, de cierre a cierre (leadlag). n = sesiones de la historia."""
        if b not in self._mbase:
            bases = sorted(s["base_rate"] for s in self.by_id.values()
                           if s.get("family") == "encadenamiento" and (s.get("params") or {}).get("tipo") == "operable"
                           and s["params"].get("b") == b and s["params"].get("direccion") == "compra"
                           and len(s["params"].get("legs") or []) == 1 and s.get("base_rate"))
            n = int(self.diag("n", b) or 0)
            j = self.pos.get(b)
            base_up = self.ll.get("base_up") or []
            if bases:
                self._mbase[b] = (bases[len(bases) // 2], n, OPEN_CLOSE)
            elif j is not None and j < len(base_up) and base_up[j]:
                self._mbase[b] = (base_up[j], n, CLOSE_CLOSE)
            else:
                self._mbase[b] = None
        return self._mbase[b]

    def leadlag_strategy(self, a: str, b: str, lag: int, up: bool) -> dict | None:
        """Pseudo-estrategia con la matriz leadlag (de cierre a cierre) para un par sin estrategia propia. Sin datos
        fuera de muestra, como mucho cuenta como «dudosa»."""
        i, j = self.pos.get(a), self.pos.get(b)
        block = self.ll if lag == 0 else (self.ll.get("lag1") or {})
        try:
            p = block["p_up_given_up" if up else "p_down_given_down"][i][j]
            n_all = block["n"][i][j]
            bu_a, bu_b = self.ll["base_up"][i], self.ll["base_up"][j]
        except (KeyError, IndexError, TypeError):
            return None
        base = bu_b if up else 1 - bu_b
        n = int(round(n_all * (bu_a if up else 1 - bu_a)))
        if not n or not p:
            return None
        pv = binom_sf(int(round(p * n)), n, base)
        lift = (p - base) * 100
        verdict = "pocos_datos" if n < 100 else "no_funciona" if pv >= 0.05 or lift < 2 else "dudosa"
        return {"id": f"leadlag-{a}{'_ayer' if lag else ''}-{b}", "n": n, "hit_rate": p, "base_rate": base,
                "p_value": pv, "lift_pp": lift, "verdict": verdict, "actionable": True}

    def window_base(self, wkey: str, coin: str) -> tuple[float, int, str] | None:
        """Tasa base de subida en las 4 h desde la ventana: la de las estrategias bolsa_a_cripto de compra (salida fija)
        con más días. n = días con señal conocida (compras + ventas)."""
        best = None
        for sig in ASIA + ["asia"]:
            c = self.by_id.get(f"bolsa_a_cripto-{sig}-{coin.lower()}-{wkey}-compra")
            v = self.by_id.get(f"bolsa_a_cripto-{sig}-{coin.lower()}-{wkey}-venta") or {}
            if not c or not c.get("base_rate"):
                continue
            n = int(c.get("n", 0) + v.get("n", 0))
            if best is None or n > best[1]:
                best = (c["base_rate"], n, c["id"])
        return best


def results_getter():
    """Getter para get_backtest que lee resultados.json solo cuando cambia (pesa varios MB)."""
    cache = {"mtime": None, "res": None}

    def get() -> dict | None:
        try:
            mt = backtest.results_path().stat().st_mtime
        except OSError:
            return None
        if mt != cache["mtime"]:
            cache["res"], cache["mtime"] = backtest.load_results(), mt
        return cache["res"]
    return get


# ---------- Módulo ----------
class Predictor:
    def __init__(self, bus, store, get_trend, get_orderflow, get_market, get_backtest, get_reaccion=None):
        self.bus = bus
        self.store = store
        self.get_trend, self.get_orderflow = get_trend, get_orderflow
        self.get_market, self.get_backtest = get_market, get_backtest
        self.get_reaccion = get_reaccion   # último «cripto_aperturas» (bot/reaccion.py) o None
        self.bt = BacktestIndex(None)
        self.last: dict | None = None
        self.pending: dict[str, dict] = {}     # id → {kind, target, when_utc, ends_utc} aún sin evaluar
        self.refs: dict[str, float] = {}       # id → precio de referencia (cripto al empezar; bolsa, la apertura)
        self.px = {c: deque(maxlen=PX_KEEP) for c in config.COINS}   # (ms, precio) propios para tendencia y volatilidad
        self._mkt_msg, self._mkt_prices, self._mkt_seen = None, {}, 0   # último market visto y cuándo llegó
        # Acierto acumulado por (kind, horizonte): [evaluadas, aciertos, suma de Brier]. Se rehace una vez al arrancar
        # y luego lo lleva _finish, para no releer la tabla entera cada ronda.
        self._acc_n: dict[tuple[str, str], list] = {}
        self._pruned = 0
        self._prune_from = 0     # la primera poda recorre todo; las siguientes, solo lo nuevo
        self.store.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS predictions(id TEXT, ts INTEGER, kind TEXT, target TEXT, when_utc INTEGER,
              horizon TEXT, prob_up REAL, expected_pct REAL, outcome_pct REAL, hit INTEGER, body TEXT, PRIMARY KEY(id, ts));
            CREATE INDEX IF NOT EXISTS predictions_ts ON predictions(ts);
            CREATE INDEX IF NOT EXISTS predictions_when ON predictions(when_utc);
            CREATE TABLE IF NOT EXISTS prediction_refs(id TEXT PRIMARY KEY, px REAL NOT NULL, ts INTEGER NOT NULL);
            """
        )
        self._load_pending()
        for kind, horizon, n, hits, brier, ups in self.store.db.execute(ACC_SQL):
            self._acc_n[(kind, horizon)] = [n, hits, brier, ups or 0]

    def _load_pending(self) -> None:
        """Al arrancar recupera lo que quedó sin evaluar y sus precios de referencia."""
        for pid, kind, target, when, body in self.store.db.execute(
                "SELECT id, kind, target, when_utc, body FROM predictions WHERE hit IS NULL ORDER BY ts"):
            try:
                b = json.loads(body)
            except (TypeError, ValueError):
                b = {}
            self.pending[pid] = {"kind": kind, "target": target, "when_utc": when, "ends_utc": b.get("ends_utc") or when,
                                 "measure": b.get("measure")}
        for pid, px in self.store.db.execute("SELECT id, px FROM prediction_refs"):
            if pid in self.pending:
                self.refs[pid] = px

    @staticmethod
    def _get(fn):
        try:
            return fn()
        except Exception as e:
            log.warning("Predicciones: no se pudo leer %s: %s", getattr(fn, "__name__", "datos"), e)
            return None

    def _refresh_backtest(self) -> None:
        res = self._get(self.get_backtest)
        if isinstance(res, dict) and res.get("generated") != self.bt.generated:
            self.bt = BacktestIndex(res)

    def _news(self, since: int) -> list[tuple[int, dict]]:
        """Noticias triadas desde `since`, una por historia (la última). Las macro o geopolíticas (no de cripto, por la
        clase del prefiltro de su captada) solo si la cripto las confirmó (evento «reflejo», bot/reflejo.py); una macro
        confirmada que no nombra monedas vale para las cuatro (como en el motor, apertura._news)."""
        by: dict[str, tuple[int, dict, str | None]] = {}
        refl: dict[str, dict] = {}
        for e in self.store.events_since(since, ("triaged", "reflejo")):
            sid = e.get("story") or e.get("id")
            if e.get("kind") == "reflejo":
                refl[sid] = (e.get("patch") or {}).get("reflejo")
                continue
            tr = (e.get("patch") or {}).get("triage")
            if isinstance(tr, dict):
                by[sid] = (int(e.get("ts") or 0), tr, e.get("title"))
        try:
            cls = reflejo.clases(self.store, list(by))
        except Exception as e:   # sin clase se decide por el titular
            log.warning("Predicciones: no pude leer la clase de las noticias: %s", e)
            cls = {}
        out = []
        for sid, (ts, tr, title) in by.items():
            if not reflejo.cuenta(title, tr, refl.get(sid), cls.get(sid)):
                continue
            rep = tr.get("repercusion") if isinstance(tr.get("repercusion"), dict) else {}
            if (reflejo.macro_confirmada(title, tr, refl.get(sid), cls.get(sid))
                    and not set(rep.get("activos") or []) & set(config.COINS)):
                # macro confirmada con la media de las cuatro y sin monedas: vale para las cuatro, como en el motor
                tr = {**tr, "repercusion": {**rep, "activos": list(config.COINS)}}
            out.append((ts, tr))
        return out

    def _context(self, now: datetime) -> dict:
        t = _ms(now)
        self._refresh_backtest()
        market = self._get(self.get_market) or {}
        of = self._get(self.get_orderflow) or {}
        of_ok = not isinstance(of.get("ts"), (int, float)) or t - of["ts"] <= OF_STALE_MS
        news = self._news(t - NEWS_BIAS_H * H)
        return {"now": now, "t": t, "trend": (self._get(self.get_trend) or {}).get("markets") or {},
                "of": (of.get("coins") or {}) if of_ok else {},
                "prices": self._fresh_prices(market, t), "flow": fresh_flow(market, t),
                "news": news, "news_bias": news_bias(news, t - NEWS_BIAS_H * H),
                "reaccion": (self._get(self.get_reaccion) if self.get_reaccion else None) or {}}

    def _fresh_prices(self, market: dict, t: int) -> dict:
        """Precios de market solo si son recientes; si no, ninguno (no se fija referencia ni se evalúa con ellos).
        El mensaje no trae hora: market publica uno nuevo cada vez que cambian los precios, así que cuenta la primera
        vez que se vio. BTC cambia cada pocos segundos: 2 min sin mensaje nuevo es que el WebSocket se ha caído."""
        prices = market.get("prices") or {}
        seen = market.get("ts")
        if not isinstance(seen, (int, float)):
            if market is not self._mkt_msg or prices != self._mkt_prices:
                self._mkt_msg, self._mkt_prices, self._mkt_seen = market, dict(prices), t
            seen = self._mkt_seen
        return prices if t - seen <= PX_STALE_MS else {}

    @staticmethod
    def _futures(key: str, ctx: dict) -> dict | None:
        """Futuro de la bolsa si tiene un dato reciente (si su consulta falla, tendencia conserva el último). Sin hora
        propia del futuro, vale la de la última actualización de esa bolsa."""
        mk = ctx["trend"].get(key) or {}
        fut = mk.get("futures") or {}
        if fut.get("change_pct") is None:
            return None
        upd = fut.get("updated") or mk.get("updated")
        return fut if not isinstance(upd, (int, float)) or ctx["t"] - upd <= FUT_STALE_MS else None

    # ---- ronda

    def run_round(self, now: datetime | None = None) -> dict:
        """Genera, guarda y publica una ronda. Cada id sale una sola vez por ronda."""
        now = now or datetime.now(timezone.utc)
        ctx = self._context(now)
        items, seen = [], set()
        for it in self.predict_markets(ctx) + self.predict_crypto(ctx):
            if it["id"] not in seen:
                seen.add(it["id"])
                items.append(it)
        self._save(items, ctx["t"], ctx["prices"])
        self.last = self.bus.publish({"type": "predictions", "ts": ctx["t"], "items": items, "accuracy": self.accuracy()})
        return self.last

    def predict_markets(self, ctx: dict) -> list[dict]:
        out = []
        for m in M.values():
            if not m.index:
                continue
            nxt = regions.session(m, ctx["now"])["next_open_utc"]
            if not nxt or _ms(nxt) - ctx["t"] > LOOKAHEAD_MS:
                continue
            try:
                out.append(self._predict_market(m, nxt, ctx))
            except Exception:
                log.exception("Predicción de %s", m.name)
        return out

    def _chain_strategy(self, a: str, lag: int, b: str, up: bool, measure: str) -> tuple[dict | None, str]:
        """Estrategia del backtest para «A cerró así → sesión de B», medida igual que la predicción: de apertura a cierre,
        la operable; de cierre a cierre (índices sin apertura fiable), la relación y, si no hay, la matriz leadlag.
        Mezclarlas engaña: la relación de cierre a cierre incluye el hueco de la apertura, que no se puede operar."""
        if measure == OPEN_CLOSE:
            return self.bt.get(f"encadenamiento-{a}{'_ayer' if lag else ''}-{b}-{'compra' if up else 'venta'}"), "operable"
        if lag == 0:
            s = self.bt.get(f"encadenamiento-{a}-{b}-{'sube' if up else 'baja'}")
            if s:   # su «actionable» es del día del backtest; aquí ya se comprobó que A cierra antes de que abra B
                return {**s, "actionable": True}, "relación de cierre a cierre"
        return self.bt.leadlag_strategy(a, b, lag, up), "leadlag de cierre a cierre"

    def _predict_market(self, m: regions.Market, nxt: datetime, ctx: dict) -> dict:
        d = nxt.astimezone(ZoneInfo(m.tz)).date()
        drivers, waiting, ignored = [], [], []
        mb = self.bt.market_base(m.key)
        base, n, measure = mb or (0.5, 0, None)
        # (i) Bolsas que ya cerraron antes de su apertura, o la sesión anterior de Nueva York. Como en el backtest, un
        # cierre cuenta 15 min después (Hong Kong cierra en subasta): si no llega antes de la apertura, no se espera.
        for a in M.values():
            if a.key == m.key or not a.index:
                continue
            if known(a.key, d, m.key, d):
                lag, a_day = 0, d
            elif a.key == LAG1:
                lag, a_day = 1, prev_weekday(d)
            else:
                continue
            ls = (ctx["trend"].get(a.key) or {}).get("last_session") or {}
            ch = ls.get("change_pct")
            if ls.get("date") != a_day.isoformat() or ch is None:
                if regions.close_utc_on(a, a_day) > ctx["now"]:   # aún no ha cerrado (si ya cerró, faltan datos)
                    waiting.append(a.name)
                continue
            if not ch:
                continue
            s, how = self._chain_strategy(a.key, lag, m.key, ch > 0, measure or OPEN_CLOSE)
            if not s:
                continue
            raw, w = edge_pp(s, ch > 0)
            if not w or abs(raw) < MIN_PP:
                ignored.append(f"{a.name} ({VERDICT_TXT.get(s.get('verdict'), 'sin ventaja')})")
                continue
            extra = "" if how == "operable" else f", {how}"
            drivers.append({
                "factor": f"{a.name} cerró {_signed(ch, 2)} %{' en la sesión anterior' if lag else ''}",
                "raw_pp": raw, "group": "encadenamiento",
                "source": (f"encadenamiento {a.key}→{m.key}: acierta el {_num(s['hit_rate'] * 100)} % frente al "
                           f"{_num(s['base_rate'] * 100)} % (n = {s['n']}, {VERDICT_TXT.get(s.get('verdict'), '')}{extra})")})
        # (ii) Futuros: su cambio sobre el cierre anterior es, antes de la apertura, casi todo el hueco de apertura.
        # Anticipa el cierre a cierre, pero no el tramo de apertura a cierre (el hueco ya está en la apertura).
        fut, fut_off = self._futures(m.key, ctx), None
        if fut:
            fut_txt = f"{fut.get('symbol') or ''} {_signed(fut['change_pct'], 2)} %".strip()
            raw = _clip(fut["change_pct"] * FUT_PP_PER_PCT, -FUT_CAP_PP, FUT_CAP_PP)
            if measure != CLOSE_CLOSE:
                fut_off = f"futuros {fut_txt} sin peso (anticipan el hueco de apertura, no el tramo de apertura a cierre)"
            elif abs(raw) >= MIN_PP:
                drivers.append({"factor": f"Futuros {fut_txt}", "raw_pp": raw,
                                "source": "futuro: cambio sobre el cierre anterior (escala fija, sin validar en el backtest)"})
        # (iii) Noticias de las últimas 12 h que nombran esta bolsa.
        raw, k, top = news_pressure(ctx["news"], lambda rep: m.key in (rep.get("bolsas") or []), ctx["t"] - NEWS_BOLSA_H * H,
                                    ctx.get("news_bias", 0.0))
        if k and abs(raw) >= MIN_PP:
            drivers.append({"factor": f"{k} noticia{'s' if k > 1 else ''} sobre {m.name} (12 h)", "raw_pp": raw,
                            "source": f"triaje de la IA local; la de más peso: «{_short(top[1])}»" + _bias_txt(ctx)})
        prob, drv = combine(base, drivers)
        parts = [f"Tasa base {_num(base * 100)} % (sesiones {MEASURE_TXT[measure]} en el backtest)" if measure
                 else "Sin backtest todavía: se parte del 50 %"]
        if waiting:
            parts.append("falta el cierre de " + _join(waiting))
        if ignored:
            parts.append("sin peso según el backtest: " + _join(ignored))
        if fut_off:
            parts.append(fut_off)
        up, down = self.bt.diag("avg_ret_up_pct", m.key), self.bt.diag("avg_ret_down_pct", m.key)
        expected = prob * up + (1 - prob) * down if up and down else (2 * prob - 1) * 0.8
        return {"id": f"{m.key}-{d.isoformat()}", "kind": "bolsa", "target": m.key, "label": f"{m.name} ({m.index_name})",
                "event": "apertura", "when_utc": _ms(nxt), "horizon": "sesión", "prob_up": round(prob, 4),
                "expected_pct": round(expected, 3), "confidence": confidence(n, [x["effect_pp"] for x in drv], prob),
                "n_samples": n, "drivers": drv, "note": "; ".join(parts) + ".",
                "base_rate": round(base, 4), "ends_utc": _ms(regions.close_utc_on(m, d)), "measure": measure or OPEN_CLOSE}

    def predict_crypto(self, ctx: dict) -> list[dict]:
        wins = upcoming_windows(ctx["now"])
        out = []
        for c in config.COINS:
            for name, when in ([wins[0]] if wins else []) + [(None, ctx["now"])]:
                try:
                    out.append(self._predict_coin(c, name, when, ctx))
                except Exception:
                    log.exception("Predicción de %s (%s)", c, name or "próxima hora")
        return out

    def _reaction_signal(self, coin: str, when: datetime, ctx: dict, ignored: list) -> list[dict]:
        """Reacción de la cripto en la apertura anterior: la última del motor cuyas 4 h ya terminaron al empezar esta
        ventana (y ahora), con el encadenamiento de bot/reaccion.py medido igual, sin mirar el futuro (la siguiente es la
        primera que empieza cuando ya se sabe ese resultado). Solo pesa si mueve ≥ 2 puntos con p < 0,05; con las cifras
        del 28-10-2026 (49,0 % tras subida frente al 50,1 %) no predice y sale en la nota con peso 0."""
        ra = ctx.get("reaccion") or {}
        ch = ra.get("chain") or {}
        if ch.get("base_rate") is None:
            return []
        limit = min(ctx["t"], _ms(when))
        best = None
        for m in (ra.get("markets") or {}).values():
            if not isinstance(m, dict) or m.get("window") not in apertura.LAB_WINDOWS:
                continue
            cands = []
            if m.get("state") == "terminada" and isinstance(m.get("open_ms"), (int, float)):
                cands.append((m["open_ms"], ((m.get("coins") or {}).get(coin) or {}).get("ret_pct")))
            last = m.get("last") or {}
            if isinstance(last.get("open_ms"), (int, float)):
                cands.append((last["open_ms"], ((last.get("coins") or {}).get(coin) or {}).get("ret_4h_pct")))
            for t0, r in cands:
                if r is not None and t0 + REACT_H * H <= limit and (best is None or t0 > best[0]):
                    best = (int(t0), float(r), m["window"])
        if not best or not best[1]:
            return []
        up = best[1] > 0
        rate, base = ch.get("after_up_rate" if up else "after_down_rate"), float(ch["base_rate"])
        n_side = int(ch.get("n_up" if up else "n_down") or (ch.get("n") or 0) // 2)
        if rate is None or not n_side:
            return []
        rate = float(rate)
        lift = rate - base
        hits = int(round(rate * n_side))
        pv = binom_sf(hits, n_side, base) if lift >= 0 else binom_sf(n_side - hits, n_side, 1 - base)
        verdict = "no_funciona" if pv >= CHAIN_P or abs(lift) * 100 < CHAIN_LIFT_PP else "dudosa"
        w = reliability({"n": n_side, "p_value": pv, "verdict": verdict, "actionable": True})
        wname = WIN_NAMES.get(best[2], best[2])
        how = (f"tras una apertura en {'positivo' if up else 'negativo'} la siguiente sube el {_num(rate * 100)} % "
               f"frente al {_num(base * 100)} % (n = {n_side})")
        raw = lift * 100 * w
        if not w or abs(raw) < MIN_PP:
            ignored.append(f"la apertura anterior ({wname}, {coin} {_signed(best[1], 2)} % a las 4 h: {how}; no predice)")
            return []
        return [{"factor": f"Apertura anterior ({wname}): {coin} {_signed(best[1], 2)} % a las 4 h", "raw_pp": raw,
                 "source": f"encadenamiento de aperturas en cripto, sin mirar el futuro: {how}"}]

    def _window_base(self, coin: str, wkey: str, ctx: dict) -> tuple[float, int, str] | None:
        """Tasa base de subida de la cripto a 4 h de la apertura: la de bot/reaccion.py (velas de 1 min, hora exacta de
        la apertura y 3 años como mucho) y, si no hay, la del backtest (bolsa_a_cripto con velas de 1 h)."""
        h = (((ctx.get("reaccion") or {}).get("markets") or {}).get(wkey) or {}).get("hist") or {}
        c = (h.get("coins") or {}).get(coin) or {}
        if c.get("up_rate") is not None and c.get("n"):
            return float(c["up_rate"]), int(c["n"]), "reaccion"
        wb = self.bt.window_base(wkey, coin)
        return (wb[0], wb[1], "backtest") if wb else None

    def _trend_signal(self, coin: str, wkey: str, t: int, ignored: list) -> list[dict]:
        """ventana_cripto «tendencia»: seguir el movimiento de las 4 h previas (solo si la ventana empieza en 1 h o menos)."""
        pre = self._move(coin, t, 4)
        s = self.bt.get(f"ventana_cripto-{wkey}-tendencia-{coin.lower()}")
        if not pre or not s:
            return []
        raw, w = edge_pp(s, pre > 0)
        if not w or abs(raw) < MIN_PP:
            ignored.append(f"tendencia de 4 h ({VERDICT_TXT.get(s.get('verdict'), 'sin ventaja')})")
            return []
        # El backtest la mide con salida a 2 h; aquí se suma a una predicción de 4 h.
        return [{"factor": f"{coin} {_signed(pre, 2)} % en las últimas 4 h", "raw_pp": raw,
                 "source": (f"ventana cripto, seguir la tendencia de las 4 h previas: a 2 h acierta el "
                            f"{_num(s['hit_rate'] * 100)} % frente al {_num(s['base_rate'] * 100)} % (n = {s['n']}, "
                            f"{VERDICT_TXT.get(s.get('verdict'), '')}); la predicción es a {WINDOW_H} h")}]

    def _predict_coin(self, coin: str, wname: str | None, when: datetime, ctx: dict) -> dict:
        t = ctx["t"]
        window = wname is not None
        wkey = WINDOW_KEY.get(wname) if window else None
        hours = WINDOW_H if window else 1
        lead_h = max(0.0, (_ms(when) - t) / H)
        drivers, ignored = [], []
        base, n, src = 0.5, 0, None
        if window and wkey:
            wb = self._window_base(coin, wkey, ctx)
            if wb:
                base, n, src = wb
            if lead_h <= 1:
                drivers += self._trend_signal(coin, wkey, t, ignored)
        if window:
            drivers += self._reaction_signal(coin, when, ctx, ignored)
        # Flujo de órdenes: el de ahora pierde peso si la ventana queda lejos.
        fe = flow_effect(ctx["of"].get(coin), ctx["prices"].get(coin))
        if fe:
            decay = 1.0 if not window else _clip(1 - (lead_h - FLOW_FULL_H) / (FLOW_ZERO_H - FLOW_FULL_H), 0, 1)
            raw = fe[0] * decay
            if abs(raw) >= MIN_PP:
                drivers.append({"factor": fe[1], "raw_pp": raw,
                                "source": "libro de OKX EU y operaciones de Binance"
                                          + (f" (pesa el {_num(decay * 100, 0)} %: faltan {_num(lead_h)} h)" if decay < 1 else "")})
        # Minorista extremo, en contra.
        f = ctx["flow"].get(coin) or {}
        if f.get("extreme") and f.get("label") in ("muy largo", "muy corto"):
            ls = f" (ratio L/S {_num(f['ls'], 2)})" if f.get("ls") else ""
            drivers.append({"factor": f"Minorista {f['label']}{ls}", "raw_pp": -RETAIL_PP if f["label"] == "muy largo" else RETAIL_PP,
                            "source": "posicionamiento en futuros de Binance: en contra del extremo"})
        # Noticias de la moneda (las macro, solo si la cripto las reflejó), con el tono medio del día descontado.
        raw, k, top = news_pressure(ctx["news"], lambda rep: coin in (rep.get("activos") or []), t - NEWS_CRIPTO_H * H,
                                    ctx.get("news_bias", 0.0))
        if k and abs(raw) >= MIN_PP:
            drivers.append({"factor": f"{k} noticia{'s' if k > 1 else ''} sobre {coin} ({NEWS_CRIPTO_H} h)", "raw_pp": raw,
                            "source": f"triaje de la IA local; la de más peso: «{_short(top[1])}»" + _bias_txt(ctx)})
        prob, drv = combine(base, drivers)
        if window:
            if src == "reaccion":
                note = (f"Tasa base {_num(base * 100)} %: {coin} subió en las {hours} h siguientes a la apertura en el "
                        f"{_num(base * 100)} % de {n} aperturas (velas de 1 min, 3 años como mucho)")
            else:
                note = (f"Tasa base {_num(base * 100)} %: {coin} subió en las {hours} h siguientes en el "
                        f"{_num(base * 100)} % de {n} días del backtest" if n
                        else "Sin tasa base histórica para esta ventana: se parte del 50 %")
            loc = when.astimezone(ZoneInfo(WINDOW_TZ.get(wname, "UTC")))
            if n and loc.minute and src == "backtest":   # el backtest usa velas de 1 h: la de las 09:30, desde las 09:00
                note += (f" (medida desde las {loc.hour:02d}:00 de {TZ_CITY.get(WINDOW_TZ.get(wname), 'allí')}, "
                         f"con velas de 1 h)")
            pid = f"{coin}-{wkey or 'ventana'}-{when.astimezone(timezone.utc):%Y-%m-%d}"
            label = f"{coin} · {wname}"
        else:
            note = "Sin tasa base: se parte del 50 % y cuentan el flujo, el minorista y las noticias"
            bucket = datetime.fromtimestamp(t // (ROUND_S * 1000) * ROUND_S, timezone.utc)
            pid, label = f"{coin}-1h-{bucket:%Y-%m-%dT%H:%M}", f"{coin} · próxima hora"
        if ignored:
            note += "; sin peso según el backtest: " + _join(ignored)
        expected = (2 * prob - 1) * 0.8 * self._sigma_1h(coin) * math.sqrt(hours)   # E|x| ≈ 0,8 σ
        return {"id": pid, "kind": "cripto", "target": coin, "label": label, "event": "ventana",
                "when_utc": _ms(when) if window else t, "horizon": f"{hours}h", "prob_up": round(prob, 4),
                "expected_pct": round(expected, 3), "confidence": confidence(n, [x["effect_pp"] for x in drv], prob),
                "n_samples": n, "drivers": drv, "note": note + ".", "base_rate": round(base, 4),
                "ends_utc": (_ms(when) if window else t) + hours * H}

    # ---- precios propios

    def _move(self, coin: str, t: int, hours: float) -> float | None:
        """Movimiento % de las últimas `hours` horas con los precios propios; None si no hay tanta historia."""
        buf = self.px.get(coin)
        start = t - int(hours * H)
        if not buf or buf[0][0] > start + 10 * 60_000:
            return None
        p0 = buf[0][1]
        for ts, p in buf:   # el último precio de antes del inicio (o el primero, si empieza poco después)
            if ts > start:
                break
            p0 = p
        return (buf[-1][1] / p0 - 1) * 100 if p0 else None

    def _sigma_1h(self, coin: str) -> float:
        """Volatilidad de 1 h a partir de rendimientos de 5 min propios (hace falta 1 h); si no, una típica."""
        pts, last = [], None
        for ts, p in self.px.get(coin) or ():
            if last is None or ts - last >= 300_000:
                pts.append(p)
                last = ts
        rets = [(b / a - 1) * 100 for a, b in zip(pts, pts[1:]) if a]
        sd = statistics.pstdev(rets) if len(rets) >= 12 else 0.0
        return sd * math.sqrt(12) if sd > 0 else TYPICAL_1H_PCT.get(coin, 0.6)

    # ---- registro y evaluación

    def _save(self, items: list[dict], t: int, prices: dict) -> None:
        # Tras un reinicio en el mismo tramo de 5 min, la próxima hora repetiría id con otra hora de inicio: se queda
        # la primera, que es la que tiene el precio de referencia.
        items = [it for it in items
                 if (self.pending.get(it["id"]) or {}).get("when_utc", it["when_utc"]) == it["when_utc"]]
        self.store.db.executemany(
            "INSERT OR REPLACE INTO predictions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(it["id"], t, it["kind"], it["target"], it["when_utc"], it["horizon"], it["prob_up"], it["expected_pct"],
              None, None, json.dumps(it, ensure_ascii=False)) for it in items])
        self.store.db.commit()
        for it in items:
            self.pending[it["id"]] = {"kind": it["kind"], "target": it["target"], "when_utc": it["when_utc"],
                                      "ends_utc": it["ends_utc"], "measure": it.get("measure")}
            if it["kind"] == "cripto" and it["when_utc"] <= t and prices.get(it["target"]):
                self._set_ref(it["id"], float(prices[it["target"]]), t)

    def _set_ref(self, pid: str, px: float, t: int) -> None:
        if pid in self.refs:
            return
        self.refs[pid] = px
        self.store.db.execute("INSERT OR IGNORE INTO prediction_refs VALUES (?,?,?)", (pid, px, t))
        self.store.db.commit()

    def tick(self, now: datetime | None = None) -> None:
        """Guarda precios propios, toma precios de referencia y evalúa lo que ya pasó su horizonte."""
        now = now or datetime.now(timezone.utc)
        t = _ms(now)
        prices = self._fresh_prices(self._get(self.get_market) or {}, t)
        for c, p in prices.items():
            buf = self.px.get(c)
            if buf is not None and p and (not buf or t > buf[-1][0]):
                buf.append((t, float(p)))
        trend = (self._get(self.get_trend) or {}).get("markets") or {}
        for pid, pend in list(self.pending.items()):
            try:
                if pend["kind"] == "bolsa":
                    self._advance_market(pid, pend, t, trend)
                else:
                    self._advance_coin(pid, pend, t, prices)
            except Exception as e:
                log.warning("Evaluación de %s: %s", pid, e)
        if t - self._pruned >= H:
            self._pruned = t
            self._prune(t)

    def _advance_coin(self, pid: str, pend: dict, t: int, prices: dict) -> None:
        """Cripto: retorno desde when_utc con los precios de market. Si el bot no estaba al empezar o al acabar
        (más de 15 min tarde), no se evalúa."""
        px = prices.get(pend["target"])
        when, ends = pend["when_utc"], pend["ends_utc"]
        if pid not in self.refs and t >= when:
            if px and t - when <= LATE_MS:
                self._set_ref(pid, float(px), t)
            elif t - when > LATE_MS:
                return self._finish(pid, None)
        if t >= ends and pid in self.refs:
            if px and t - ends <= LATE_MS:
                return self._finish(pid, (float(px) / self.refs[pid] - 1) * 100)
            if t - ends > LATE_MS:
                return self._finish(pid, None)

    def _advance_market(self, pid: str, pend: dict, t: int, trend: dict) -> None:
        """Bolsa: de la apertura (primera vela de 5 min de la sesión) al cierre, con lo último del trend. Si la
        predicción es de cierre a cierre (índice sin apertura fiable), el cambio de la sesión sobre el cierre anterior.
        Si es de apertura a cierre y no se conoce la apertura, no se evalúa: mezclaría dos medidas distintas."""
        m = M.get(pend["target"])
        if m is None:
            return self._finish(pid, None)
        mk = trend.get(m.key) or {}
        when, ends = pend["when_utc"], pend["ends_utc"]
        d = datetime.fromtimestamp(when / 1000, ZoneInfo(m.tz)).date().isoformat()
        spark = mk.get("spark") or []
        same = mk.get("session_date") == d and bool(spark)
        if pid not in self.refs and t >= when and same and spark[0]:
            self._set_ref(pid, float(spark[0]), t)
        if t < ends + CLOSE_GRACE_MS:
            return
        ls = mk.get("last_session") or {}
        if ls.get("date") == d and ls.get("change_pct") is not None:
            if pend.get("measure") == CLOSE_CLOSE:
                return self._finish(pid, ls["change_pct"])
            open_px = (spark[0] if same else None) or self.refs.get(pid)
            close = ls.get("close") or (spark[-1] if same else None)
            return self._finish(pid, (close / open_px - 1) * 100 if open_px and close else None)
        if t - ends > EXPIRE_MS:
            self._finish(pid, None)

    def _finish(self, pid: str, outcome: float | None) -> None:
        """Marca outcome y hit en todas las rondas de esa predicción. hit = −1 si no se pudo evaluar o no hubo
        dirección (prob_up = 0,5 o movimiento nulo); no cuenta en el acierto."""
        db = self.store.db
        old = db.execute(FINAL_ONE_SQL, (pid,)).fetchone()   # por si ya estaba evaluada: no contar dos veces
        if outcome is None or outcome == 0:
            db.execute("UPDATE predictions SET outcome_pct=?, hit=-1 WHERE id=?", (outcome, pid))
        else:
            up = 1 if outcome > 0 else 0
            db.execute("UPDATE predictions SET outcome_pct=?, hit=CASE WHEN prob_up > 0.5 THEN ? WHEN prob_up < 0.5 "
                       "THEN ? ELSE -1 END WHERE id=?", (round(outcome, 4), up, 1 - up, pid))
        new = db.execute(FINAL_ONE_SQL, (pid,)).fetchone()
        db.execute("DELETE FROM prediction_refs WHERE id=?", (pid,))
        db.commit()
        self._count(old, -1)
        self._count(new, 1)
        self.pending.pop(pid, None)
        self.refs.pop(pid, None)
        log.debug("Predicción %s evaluada: %s", pid, "sin datos" if outcome is None else f"{outcome:+.2f} %")

    def _count(self, row, sign: int) -> None:
        """Suma (o resta) al acierto acumulado la ronda final de una predicción, si tiene acierto (hit 0 o 1)."""
        if not row or row[4] is None or row[4] < 0:
            return
        kind, horizon, prob, outcome, hit = row
        c = self._acc_n.setdefault((kind, horizon), [0, 0, 0.0, 0])
        c[0] += sign
        c[1] += sign * hit
        c[2] += sign * (prob - (outcome > 0)) ** 2
        c[3] += sign * (outcome > 0)

    def _prune(self, t: int) -> None:
        """Borra las rondas intermedias de más de 14 días; de cada predicción se queda la última antes del evento.
        Tras la primera poda solo se repasa lo que ha cumplido 14 días desde la anterior (con 7 días de solape)."""
        cut = t - PRUNE_MS
        self.store.db.execute(
            "DELETE FROM predictions WHERE ts < ? AND ts >= ? AND hit IS NOT NULL AND ts < (SELECT MAX(q.ts) "
            "FROM predictions q WHERE q.id = predictions.id AND q.ts <= q.when_utc)", (cut, self._prune_from))
        self.store.db.commit()
        self._prune_from = cut - 7 * 86400_000

    def accuracy(self) -> dict:
        """Acierto de la última predicción de cada id antes de su evento (la que se habría usado para decidir), con la
        proporción de subidas (up_rate) para compararlo con las bases ingenuas «siempre sube» y «siempre baja»."""
        keys = [k for k, c in self._acc_n.items() if c[0] > 0]

        def total(ks) -> list:
            return [sum(self._acc_n[k][i] for k in ks) for i in range(4)]

        def agg(c) -> dict:
            # up_rate: proporción de veces que subió; «siempre sube» acierta up_rate y «siempre baja», 1 − up_rate.
            return {"evaluated": c[0], "hit_rate": round(c[1] / c[0], 4) if c[0] else 0.0,
                    "up_rate": round(c[3] / c[0], 4) if c[0] else None}

        everything = total(keys)
        acc = agg(everything)
        acc["by_kind"] = {kd: agg(total([k for k in keys if k[0] == kd])) for kd in ("bolsa", "cripto")}
        acc["by_horizon"] = {h: agg(total([k for k in keys if k[1] == h])) for h in sorted({k[1] for k in keys})}
        acc["brier"] = round(everything[2] / everything[0], 4) if everything[0] else None
        return acc

    def evaluated(self, n: int = 50) -> list[dict]:
        """Últimas predicciones ya evaluadas (la ronda final de cada una), con su resultado real. Recorre el índice de
        when_utc desde la más reciente y para al llegar a n: tarda milisegundos aunque la tabla sea grande."""
        rows = self.store.db.execute(EVALUATED_SQL, (n,)).fetchall()
        return [{**json.loads(r[2]), "outcome_pct": r[0], "hit": r[1]} for r in rows]

    def view(self) -> dict:
        """Para GET /api/predicciones."""
        return self.last or {"type": "predictions", "ts": None, "items": [], "accuracy": self.accuracy()}

    async def run(self) -> None:
        await asyncio.sleep(START_DELAY_S)
        last_round = -1e9
        while True:
            try:
                self.tick()
                if time.monotonic() - last_round >= ROUND_S:
                    last_round = time.monotonic()
                    self.run_round()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Predicciones: %s", e)
            await asyncio.sleep(TICK_S)
