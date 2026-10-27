"""Tendencia en directo: índices de las bolsas (gráfico público de Yahoo Finance) y flujo de órdenes de cripto
(libro de OKX EU y operaciones al contado de Binance). Publica «trend» y «orderflow» (contrato 2.2 y 2.3)."""
import asyncio
import json
import logging
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import aiohttp

from . import config, regions
from .bus import Bus
from .models import now_ms

log = logging.getLogger("trend")

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/"
YAHOO_HEADERS = {   # con el User-Agent de Python Yahoo responde 429
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}
OKX_WS = "wss://wseea.okx.com:8443/ws/v5/public"
BN_WS = "wss://data-stream.binance.vision/stream?streams="
BN_KLINES = "https://data-api.binance.vision/api/v3/klines"
VENUE = "OKX EU (libro) + Binance (operaciones)"

INDEX_EVERY_S = 60
MAX_WAIT_S = 600             # espera máxima entre rondas de Yahoo cuando falla o responde 429
DAILY_EVERY_S = 3600
DAILY_SETTLE_S = 30 * 60     # el diario se vuelve a pedir media hora después del cierre, para tener el cierre definitivo
REQ_GAP_S = 0.5
ORDERFLOW_EVERY_S = 5
SPARK_MAX = 120
HOLIDAY_GRACE_S = 45 * 60    # sin velas de hoy 45 min después de abrir = festivo (hay índices con 15-20 min de retraso)
FRESH_INTRA_S = MAX_WAIT_S + INDEX_EVERY_S   # el aviso de festivo solo se da con datos recientes (aguanta la espera)
# Subasta de cierre que termina después de la hora de cierre: hasta entonces Yahoo da el precio de antes de la subasta
# (HKEX 16:08-16:10, LSE 16:35, BME 17:35; comprobado con el regularMarketTime final de cada índice).
CLOSE_AUCTION_S = {"hongkong": 480, "londres": 300, "madrid": 300}
WINDOWS_S = (60, 300, 900, 3600)
TREND_RET_PCT = 0.15
STALE_TRADES_S = 120         # sin operaciones en este tiempo las métricas dejan de valer
STALE_BOOK_S = 60
GAP_S = 90                   # más de 90 s sin operaciones es un hueco en los datos
REF_SLACK_S = 60             # el precio de referencia de un retorno puede ser hasta 60 s más viejo que la ventana (velas de 1 min)


class RateLimited(ConnectionError):
    """Yahoo responde HTTP 429: hay que espaciar las rondas."""


def _ms(dt: datetime | None) -> int | None:
    return int(dt.timestamp() * 1000) if dt else None


def _pct(a: float | None, b: float | None) -> float | None:
    return round((a / b - 1) * 100, 3) if a is not None and b else None


def _round(x: float | None, d: int = 2) -> float | None:
    return round(x, d) if x is not None else None


# ---------------------------------------------------------------- índices (Yahoo Finance)

def _result(data: dict) -> dict:
    chart = (data or {}).get("chart") or {}
    if chart.get("error"):
        e = chart["error"]
        raise ValueError(f"Yahoo: {e.get('description') or e.get('code')}")
    res = chart.get("result") or []
    if not res:
        raise ValueError("Yahoo: respuesta sin datos")
    return res[0]


def _candles(r: dict) -> list[tuple]:
    """Velas con cierre válido: (ts_s, open, high, low, close). Yahoo deja a null la pausa de mediodía."""
    ts = r.get("timestamp") or []
    q = (((r.get("indicators") or {}).get("quote")) or [{}])[0] or {}
    cols = [q.get(k) or [] for k in ("open", "high", "low", "close")]
    out = []
    for i, t in enumerate(ts):
        o, h, lo, c = (col[i] if i < len(col) else None for col in cols)
        if c is not None:
            out.append((t, o, h, lo, c))
    return out


def parse_chart(data: dict, market: regions.Market | None = None) -> dict:
    """Gráfico de 5 min de Yahoo (range=1d) → campos de precio de «trend». Sin velas (festivo) usa el último precio
    y el cierre anterior de la cabecera. El estado de la sesión se calcula aparte con market_state()."""
    r = _result(data)
    meta = r.get("meta") or {}
    tz = ZoneInfo(market.tz if market else meta.get("exchangeTimezoneName") or "UTC")
    cs = _candles(r)
    d = meta.get("priceHint") if isinstance(meta.get("priceHint"), int) else 2   # Yahoo puede mandarlo a null
    price = meta.get("regularMarketPrice") or (cs[-1][4] if cs else None)
    # Sin velas, chartPreviousClose se refiere a un periodo vacío; previousClose es el cierre anterior al último precio.
    prev = meta.get("chartPreviousClose") if cs else (meta.get("previousClose") or meta.get("chartPreviousClose"))
    highs = [c[2] for c in cs if c[2] is not None]
    lows = [c[3] for c in cs if c[3] is not None]
    high = meta.get("regularMarketDayHigh") or (max(highs) if highs else None)
    low = meta.get("regularMarketDayLow") or (min(lows) if lows else None)
    mom = None
    if cs:   # últimas 6 velas: cierre de la última frente al cierre previo a esas 6 (o la apertura de la sesión)
        ref = cs[-7][4] if len(cs) > 6 else (cs[0][1] or cs[0][4])
        mom = _pct(cs[-1][4], ref)
    rmt = meta.get("regularMarketTime")
    return {"symbol": meta.get("symbol"), "price": _round(price, d), "prev_close": _round(prev, d),
            "change_pct": _pct(price, prev), "day_high": _round(high, d), "day_low": _round(low, d),
            "mom_30m_pct": mom, "spark": [_round(c[4], d) for c in cs[-SPARK_MAX:]],
            "session_date": datetime.fromtimestamp(cs[-1][0], tz).date().isoformat() if cs else None,
            "last_candle": cs[-1][0] * 1000 if cs else None, "data_ts": rmt * 1000 if rmt else None}


def session_final(m: regions.Market, day: date, data_ms: int | None) -> bool:
    """¿El último dato de Yahoo (regularMarketTime) ya es el cierre de ese día, subasta de cierre incluida? Con un
    gráfico que lleva horas fallando, un índice retrasado o en subasta, el precio aún es de antes del cierre."""
    close_ms = _ms(regions.close_utc_on(m, day)) + CLOSE_AUCTION_S.get(m.key, 0) * 1000
    return bool(data_ms) and data_ms >= close_ms


def parse_daily(data: dict, market: regions.Market) -> list[dict]:
    """Gráfico diario de Yahoo (range=5d) → [{date, close, change_pct, final}] por fecha local de la bolsa. La fila de
    hoy solo es final si el último dato de Yahoo es del cierre: durante la sesión Yahoo la deja a null o con el precio
    del momento."""
    r = _result(data)
    meta = r.get("meta") or {}
    tz = ZoneInfo(market.tz)
    closes = ((((r.get("indicators") or {}).get("quote")) or [{}])[0] or {}).get("close") or []
    day = lambda t: datetime.fromtimestamp(t, tz).date().isoformat()
    rows: dict[str, float | None] = {}
    for t, c in zip(r.get("timestamp") or [], closes):
        rows[day(t)] = c if c is not None else rows.get(day(t))
    rmt, price = meta.get("regularMarketTime"), meta.get("regularMarketPrice")
    last = max(rows) if rows else None
    final = {d: session_final(market, date.fromisoformat(d), rmt * 1000) if rmt else d != last for d in rows}
    # Yahoo deja a null el cierre diario de hoy durante horas: se rellena con el último precio si ya es el del cierre.
    if last and rows[last] is None and price and final[last] and day(rmt) == last:
        rows[last] = price
    out, prev = [], meta.get("chartPreviousClose")
    for d in sorted(rows):
        c = rows[d]
        if c is None:
            continue
        out.append({"date": d, "close": round(c, 2), "change_pct": _pct(c, prev), "final": final[d]})
        prev = c
    return out


def last_session(days: list[dict] | None, market: regions.Market, now: datetime, intra: dict | None = None) -> dict | None:
    """Última sesión completa: la más reciente entre el gráfico de 5 min (se refresca cada minuto) y el diario, pero
    solo si su dato es del cierre (session_final). Hasta entonces sigue la anterior, y predicciones espera."""
    today = now.astimezone(ZoneInfo(market.tz)).date()
    iso = today.isoformat()
    closed_today = regions.is_trading_day(market, today) and now >= regions.close_utc_on(market, today)
    done = lambda d: d < iso or (closed_today and d == iso)
    found = []
    sd = (intra or {}).get("session_date")
    if (sd and done(sd) and intra.get("change_pct") is not None
            and session_final(market, date.fromisoformat(sd), intra.get("data_ts"))):
        found.append({"date": sd, "change_pct": intra["change_pct"], "close": intra.get("price")})
    for d in reversed(days or []):
        if d.get("final", True) and done(d["date"]) and d.get("change_pct") is not None:
            found.append({"date": d["date"], "change_pct": d["change_pct"], "close": d["close"]})
            break
    return max(found, key=lambda s: s["date"]) if found else None


def market_state(m: regions.Market, now: datetime, last_candle_ms: int | None = None, checked: bool = False) -> dict:
    """Estado según el horario (regions.session) corregido con los datos: si 45 min después de la apertura no hay
    ninguna vela de hoy, es festivo y la bolsa está cerrada. Solo se corrige con datos recientes (checked)."""
    s = regions.session(m, now)
    state, holiday, close = s["state"], False, s["close_utc"]
    o = s["open_utc"]
    if checked and o and now - o >= timedelta(seconds=HOLIDAY_GRACE_S) and (last_candle_ms or 0) < _ms(o):
        state, holiday, close = "cerrada", True, None
    return {"state": state, "holiday": holiday, "close_utc": _ms(close), "next_open_utc": _ms(s["next_open_utc"])}


# ---------------------------------------------------------------- libro y operaciones (cripto)

def imbalance(book: dict) -> dict:
    """books5 de OKX (un elemento de "data", o el mensaje entero) → desequilibrio de los 5 primeros niveles."""
    if "data" in book:
        book = (book.get("data") or [{}])[0]
    bids = [(float(p), float(q)) for p, q, *_ in (book.get("bids") or [])[:5]]
    asks = [(float(p), float(q)) for p, q, *_ in (book.get("asks") or [])[:5]]
    bv, av = sum(q for _, q in bids), sum(q for _, q in asks)
    mid = (bids[0][0] + asks[0][0]) / 2 if bids and asks else None
    return {"imbalance": round((bv - av) / (bv + av), 4) if bv + av else None,
            "bid_vol": round(bv, 6), "ask_vol": round(av, 6),
            "spread_bps": round((asks[0][0] - bids[0][0]) / mid * 10000, 3) if mid else None,
            "mid": mid, "book_ts": int(book.get("ts") or 0)}


def parse_agg_trade(msg: dict) -> tuple | None:
    """aggTrade de Binance → (moneda, ts_ms, precio, cantidad, venta_agresiva). Si m es true el comprador es maker,
    así que quien cruzó el diferencial fue el vendedor."""
    d = msg.get("data", msg)
    if d.get("e") != "aggTrade":
        return None
    s = d.get("s", "")
    coin = s[:-4] if s.endswith("USDT") else s
    return coin, int(d["T"]), float(d["p"]), float(d["q"]), bool(d["m"])


def trend_label(ret_15m: float | None, cvd_15m: float | None) -> str:
    if ret_15m is None or cvd_15m is None:
        return "lateral"
    if ret_15m > TREND_RET_PCT and cvd_15m > 0:
        return "alcista"
    if ret_15m < -TREND_RET_PCT and cvd_15m < 0:
        return "bajista"
    return "lateral"


def _empty_metrics() -> dict:
    out = {f"cvd_{w // 60}m": None for w in WINDOWS_S}
    out.update({"buy_ratio_5m": None, "vol_5m_usd": None, "vol_ratio": None, "ret_5m_pct": None,
                "ret_15m_pct": None, "ret_60m_pct": None, "price": None, "trend": None})
    return out


class OrderFlowWindow:
    """Operaciones de una moneda agregadas por segundo durante algo más de una hora. La memoria queda acotada
    (como mucho KEEP_S cubetas) aunque lleguen cientos de operaciones por segundo."""
    KEEP_S = 3600 + 120   # un poco más de 1 h para tener el precio de referencia del retorno de 60 min

    def __init__(self):
        self.b: deque[list] = deque(maxlen=self.KEEP_S + 60)   # [segundo, compra, venta, compra_usd, venta_usd, precio]
        self.last_ms = 0

    def add(self, ts_ms: int, price: float, qty: float, sell: bool) -> None:
        sec = int(ts_ms // 1000)
        b = self.b
        if b and sec <= b[-1][0]:   # mismo segundo (o llega desordenada): a la última cubeta
            cur = b[-1]
        else:
            cur = [sec, 0.0, 0.0, 0.0, 0.0, price]
            b.append(cur)
            while b[0][0] < sec - self.KEEP_S:
                b.popleft()
        usd = price * qty
        if sell:
            cur[2] += qty
            cur[4] += usd
        else:
            cur[1] += qty
            cur[3] += usd
        if sec >= cur[0]:
            cur[5] = price
        self.last_ms = max(self.last_ms, ts_ms)

    def seed_klines(self, rows: list, now: int) -> None:
        """Rellena la última hora con velas de 1 min de Binance (taker buy = compras agresivas) al arrancar o tras un
        corte. La vela en curso se descarta: la completan las operaciones en directo."""
        self.b.clear()
        self.last_ms = 0
        for k in rows:
            if int(k[6]) >= now:
                continue
            v, qv, tb, tbq = float(k[5]), float(k[7]), float(k[9]), float(k[10])
            self.b.append([int(k[0]) // 1000 + 59, tb, v - tb, tbq, qv - tbq, float(k[4])])
            self.last_ms = int(k[6])

    def metrics(self, now: int) -> dict:
        """CVD (en unidades de la moneda), compras, volumen y retornos en ventanas de 1, 5, 15 y 60 min. Tras un hueco
        (corte sin relleno) el retorno de una ventana es null si no hay precio de hace w segundos, y la media de
        volumen de vol_ratio solo cuenta el tramo continuo más reciente."""
        if not self.b or now - self.last_ms > STALE_TRADES_S * 1000:
            return _empty_metrics()
        now_s = now / 1000
        cvd = dict.fromkeys(WINDOWS_S, 0.0)
        buy = dict.fromkeys(WINDOWS_S, 0.0)
        sell = dict.fromkeys(WINDOWS_S, 0.0)
        ref: dict[int, float | None] = {}   # precio más reciente con al menos w segundos (y no mucho más) de antigüedad
        seg_start, seg_vol, nxt, cont = now_s, 0.0, None, True   # tramo continuo más reciente (sin huecos > GAP_S)
        for sec, bq, sq, bu, su, px in reversed(self.b):
            age = now_s - sec
            cont = cont and (nxt is None or nxt - sec <= GAP_S)
            nxt = sec
            if cont and age < 3600:
                seg_start, seg_vol = sec, seg_vol + bu + su
            for w in WINDOWS_S:
                if age < w:
                    cvd[w] += bq - sq
                    buy[w] += bu
                    sell[w] += su
                elif w not in ref:
                    ref[w] = px if age <= w + REF_SLACK_S else None
        last = self.b[-1][5]
        vol5 = buy[300] + sell[300]
        covered = min(3600.0, now_s - seg_start)
        avg5 = seg_vol / (covered / 300) if covered >= 600 else 0
        out = {f"cvd_{w // 60}m": round(cvd[w], 4) for w in WINDOWS_S}
        out.update({"buy_ratio_5m": round(buy[300] / vol5, 4) if vol5 else None, "vol_5m_usd": round(vol5),
                    "vol_ratio": round(vol5 / avg5, 3) if avg5 else None,
                    "ret_5m_pct": _pct(last, ref.get(300)), "ret_15m_pct": _pct(last, ref.get(900)),
                    "ret_60m_pct": _pct(last, ref.get(3600)), "price": last})
        out["trend"] = trend_label(out["ret_15m_pct"], out["cvd_15m"])
        return out


# ---------------------------------------------------------------- módulo

class Trend:
    def __init__(self, bus: Bus, session: aiohttp.ClientSession):
        self.bus = bus
        self.s = session
        self.last_trend: dict = {}
        self.last_orderflow: dict = {}
        self.ok = {"indices": None, "books": None, "trades": None}
        self.err = {"indices": "", "books": "", "trades": ""}
        self.intra: dict[str, dict] = {}       # último gráfico de 5 min por bolsa
        self.intra_at: dict[str, float] = {}
        self.daily: dict[str, list] = {}
        self.daily_at: dict[str, float] = {}
        self.daily_ms: dict[str, int] = {}     # hora (UTC) de la ronda en que se pidió el diario
        self.futures: dict[str, dict] = {}
        self.errors: dict[str, str | None] = {}
        self.updated: dict[str, int] = {}
        self.books: dict[str, dict] = {}       # último books5 por moneda, con la hora de llegada
        self.windows = {c: OrderFlowWindow() for c in config.COINS}

    async def run(self) -> None:
        await asyncio.gather(self._indices_loop(), self._books_loop(), self._trades_loop(), self._orderflow_loop())

    # ---- índices

    @staticmethod
    def _markets() -> list[regions.Market]:
        return [m for m in regions.MARKETS.values() if m.index]

    async def _yahoo(self, symbol: str, rng: str, interval: str) -> dict:
        url = YAHOO + quote(symbol, safe="")
        async with self.s.get(url, params={"interval": interval, "range": rng}, headers=YAHOO_HEADERS) as r:
            if r.status == 429:
                raise RateLimited("HTTP 429")
            if r.status != 200:
                raise ConnectionError(f"HTTP {r.status}")
            return await r.json(content_type=None)

    def _daily_due(self, m: regions.Market, now: datetime) -> bool:
        """El diario se pide cada hora y otra vez media hora después del cierre de hoy si el último es de antes
        (sería de mitad de sesión y no tendría el cierre)."""
        if time.monotonic() - self.daily_at.get(m.key, -1e9) >= DAILY_EVERY_S:
            return True
        day = now.astimezone(ZoneInfo(m.tz)).date()
        if not regions.is_trading_day(m, day):
            return False
        settle = _ms(regions.close_utc_on(m, day)) + DAILY_SETTLE_S * 1000
        return _ms(now) >= settle and self.daily_ms.get(m.key, 0) < settle

    async def refresh_indices(self, now: datetime | None = None) -> tuple[int, int, int]:
        """Una ronda: 5 min de cada índice y su futuro; el diario de 5 días cada hora y tras el cierre. Devuelve
        (fallos, consultas, respuestas 429). Si una consulta falla se guarda el motivo y se conservan los últimos
        datos de esa bolsa."""
        now = now or datetime.now(timezone.utc)
        fails = total = limited = 0
        for m in self._markets():
            jobs = [("intra", m.index, "1d", "5m")]
            if m.futures:
                jobs.append(("fut", m.futures, "1d", "5m"))
            if self._daily_due(m, now):
                jobs.append(("daily", m.index, "5d", "1d"))
            errs = []
            for kind, sym, rng, interval in jobs:
                total += 1
                try:
                    data = await self._yahoo(sym, rng, interval)
                    if kind == "intra":
                        self.intra[m.key] = parse_chart(data, m)
                        self.intra_at[m.key] = time.monotonic()
                        self.updated[m.key] = now_ms()
                    elif kind == "fut":
                        f = parse_chart(data)
                        self.futures[m.key] = {"symbol": sym, "change_pct": f["change_pct"], "price": f["price"]}
                    else:
                        self.daily[m.key] = parse_daily(data, m)
                        self.daily_at[m.key] = time.monotonic()
                        self.daily_ms[m.key] = _ms(now)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    fails += 1
                    limited += isinstance(e, RateLimited)
                    what = {"intra": "gráfico de 5 min", "fut": f"futuro {sym}", "daily": "diario"}[kind]
                    errs.append(f"{what}: {(str(e) or type(e).__name__)[:120]}")
                    log.warning("Tendencia %s (%s): %s", m.name, what, errs[-1])
                await asyncio.sleep(REQ_GAP_S)
            self.errors[m.key] = " · ".join(errs) or None
        return fails, total, limited

    def build_trend(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        out = {}
        for m in self._markets():
            intra = self.intra.get(m.key) or {}
            fresh = bool(intra) and time.monotonic() - self.intra_at.get(m.key, -1e9) < FRESH_INTRA_S
            st = market_state(m, now, intra.get("last_candle"), checked=fresh)
            fut = (self.futures.get(m.key) or {"symbol": m.futures, "change_pct": None, "price": None}) if m.futures else None
            out[m.key] = {
                "name": m.name, "index": m.index_name, "symbol": m.index, "state": st["state"],
                "price": intra.get("price"), "prev_close": intra.get("prev_close"), "change_pct": intra.get("change_pct"),
                "day_high": intra.get("day_high"), "day_low": intra.get("day_low"), "mom_30m_pct": intra.get("mom_30m_pct"),
                "last_session": last_session(self.daily.get(m.key), m, now, intra or None),
                "futures": fut, "next_open_utc": st["next_open_utc"], "close_utc": st["close_utc"],
                "spark": intra.get("spark", []), "updated": self.updated.get(m.key), "error": self.errors.get(m.key),
                # Añadidos: festivo detectado, fecha de las velas y hora del último dato de Yahoo.
                "holiday": st["holiday"], "session_date": intra.get("session_date"), "data_ts": intra.get("data_ts")}
        return {"type": "trend", "ts": now_ms(), "markets": out}

    async def _indices_loop(self) -> None:
        """Ronda de Yahoo cada 60 s, con espera creciente si falla más de la mitad o hay algún 429. «trend» se publica
        cada 60 s aunque se salte la ronda, para que los estados por horario sigan al día."""
        wait, next_round = INDEX_EVERY_S, 0.0
        while True:
            t0 = time.monotonic()
            if t0 >= next_round:
                try:
                    fails, total, limited = await self.refresh_indices()
                    self.ok["indices"] = fails < total
                    self.err["indices"] = (f"{fails} de {total} consultas fallaron"
                                           + (f" ({limited} por límite de peticiones)" if limited else "")) if fails else ""
                    wait = min(wait * 2, MAX_WAIT_S) if limited or fails * 2 > total else INDEX_EVERY_S
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.ok["indices"], self.err["indices"] = False, (str(e) or type(e).__name__)[:160]
                    log.warning("Tendencia de índices: %s", self.err["indices"])
                    wait = min(wait * 2, MAX_WAIT_S)
                next_round = t0 + wait
            try:
                self.last_trend = self.bus.publish(self.build_trend())
            except Exception as e:
                log.warning("Tendencia de índices (publicación): %s", e)
            await asyncio.sleep(max(1.0, min(next_round, t0 + INDEX_EVERY_S) - time.monotonic()))

    # ---- libro de OKX EU

    async def _books_loop(self) -> None:
        sub = {"op": "subscribe", "args": [{"channel": "books5", "instId": f"{c}-{config.QUOTE}"} for c in config.COINS]}
        wait = 2
        while True:
            try:
                async with self.s.ws_connect(OKX_WS, heartbeat=20) as ws:
                    await ws.send_json(sub)
                    log.info("Libro books5 de OKX EU conectado")
                    last_ping = time.monotonic()
                    while True:
                        msg = await ws.receive(timeout=30)
                        if time.monotonic() - last_ping >= 20:
                            await ws.send_str("ping")   # OKX corta si pasan 30 s sin mensajes nuestros
                            last_ping = time.monotonic()
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                                raise ConnectionError(f"WebSocket cerrado ({msg.type.name})")
                            continue
                        if msg.data == "pong":
                            continue
                        d = json.loads(msg.data)
                        if d.get("event") == "error":
                            raise ConnectionError(d.get("msg") or "error de suscripción")
                        coin = ((d.get("arg") or {}).get("instId") or "").split("-")[0]
                        for book in d.get("data") or []:
                            self.books[coin] = {**imbalance(book), "rx": now_ms()}
                            self.ok["books"], self.err["books"], wait = True, "", 2
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.ok["books"], self.err["books"] = False, (str(e) or type(e).__name__)[:160]
                log.warning("Libro OKX: %s (reintento en %d s)", self.err["books"], wait)
                await asyncio.sleep(wait)
                wait = min(wait * 2, 120)

    # ---- operaciones de Binance

    async def _seed_windows(self) -> None:
        """Tras arrancar o tras un corte de más de 90 s, rellena la última hora con velas de 1 min."""
        now = now_ms()
        for c, w in self.windows.items():
            if w.last_ms and now - w.last_ms < 90_000:
                continue
            try:
                async with self.s.get(BN_KLINES, params={"symbol": f"{c}USDT", "interval": "1m", "limit": "61"}) as r:
                    r.raise_for_status()
                    w.seed_klines(await r.json(content_type=None), now)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Historial de 1 min de %s no disponible: %s", c, e)

    async def _trades_loop(self) -> None:
        url = BN_WS + "/".join(f"{c.lower()}usdt@aggTrade" for c in config.COINS)
        wait = 2
        while True:
            try:
                await self._seed_windows()
                async with self.s.ws_connect(url) as ws:
                    log.info("Operaciones de Binance conectadas")
                    while True:
                        msg = await ws.receive(timeout=30)
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                                raise ConnectionError(f"WebSocket cerrado ({msg.type.name})")
                            continue
                        t = parse_agg_trade(json.loads(msg.data))
                        if t and t[0] in self.windows:
                            self.windows[t[0]].add(*t[1:])
                            self.ok["trades"], self.err["trades"], wait = True, "", 2
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.ok["trades"], self.err["trades"] = False, (str(e) or type(e).__name__)[:160]
                log.warning("Operaciones Binance: %s (reintento en %d s)", self.err["trades"], wait)
                await asyncio.sleep(wait)
                wait = min(wait * 2, 120)

    # ---- publicación

    def build_orderflow(self, now: int | None = None) -> dict:
        now = now or now_ms()
        coins = {}
        for c in config.COINS:
            bk = self.books.get(c)
            row = {"imbalance": None, "bid_vol": None, "ask_vol": None, "spread_bps": None}
            if bk and now - bk["rx"] <= STALE_BOOK_S * 1000:
                row.update({k: bk[k] for k in row})
            row.update(self.windows[c].metrics(now))
            row["venue"] = VENUE
            coins[c] = row
        return {"type": "orderflow", "ts": now, "coins": coins,
                "status": {k: {"ok": self.ok[k], "err": self.err[k]} for k in ("books", "trades")}}

    async def _orderflow_loop(self) -> None:
        while True:
            await asyncio.sleep(ORDERFLOW_EVERY_S)
            if not self.books and not any(w.b for w in self.windows.values()):
                continue   # aún no ha llegado nada
            try:
                self.last_orderflow = self.bus.publish(self.build_orderflow())
            except Exception as e:
                log.warning("Flujo de órdenes: %s", e)

    def view(self) -> dict:
        """Para GET /api/tendencia."""
        return {"trend": self.last_trend or None, "orderflow": self.last_orderflow or None}

    def sources(self) -> list[dict]:
        """Filas para el mensaje «sources» (mismo formato que las de market en __main__.report_sources)."""
        rows = [("trend_indices", "Índices de bolsas (Yahoo Finance)", "indices", len(self.intra)),
                ("okx_books", "Libro de órdenes OKX EU", "books", len(self.books)),
                ("binance_trades", "Operaciones al contado Binance", "trades", sum(1 for w in self.windows.values() if w.b))]
        return [{"id": i, "name": n, "tier": "Mercado", "n": cnt, "ok": self.ok[k], "err": self.err[k], "lat": None}
                for i, n, k, cnt in rows]
