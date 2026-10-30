"""Precios en directo (OKX EU, pares USDC) y flujo minorista (datos públicos de futuros de Binance y Coinbase).

Funding: flow[moneda].funding es la última tasa de Binance USDT-M en % de su periodo y flow[moneda].funding_h las horas de
ese periodo (GET /fapi/v1/fundingInfo, cada 6 h: solo lista los símbolos con periodo cambiado; el resto liquida cada
8 h). Sin respuesta de Binance, funding_h no va y quien lo lea supone 8 h (paper.funding_8h)."""
import asyncio
import json
import logging
import time

import aiohttp

from . import config
from .bus import Bus
from .models import now_ms

log = logging.getLogger("market")
OKX_WS = "wss://wseea.okx.com/ws/v5/public"   # puerto 443: OKX retira el 8443 (aviso 64010, docs/06)
BN = "https://fapi.binance.com"
FUNDING_INFO_S = 6 * 3600    # el periodo de funding casi nunca cambia
FUNDING_H_DEFAULT = 8.0      # Binance liquida cada 8 h salvo los símbolos que lista fundingInfo
FLOW_STALE_MS = 5 * 60_000   # una fila del flujo minorista más vieja que esto no se usa (Binance caído; n.º 44)


def fresh_flow(market: dict | None, now: int | None = None, max_age_ms: int = FLOW_STALE_MS) -> dict:
    """Las filas de market.flow (funding, L/S…) con hora reciente («ts», puesto al leerlas de Binance): si Binance
    deja de responder, el último dato se seguía usando sin aviso en las predicciones, el motor, el paper y el estratega
    (hallazgo n.º 44). Una fila sin «ts» (de antes del 28-10-2026) se acepta."""
    now = now_ms() if now is None else now
    out = {}
    for c, f in ((market or {}).get("flow") or {}).items():
        if not isinstance(f, dict):
            continue
        ts = f.get("ts")
        if not isinstance(ts, (int, float)) or now - ts <= max_age_ms:
            out[c] = f
    return out


def funding_8h(f: dict) -> float:
    """Tasa de funding de una fila de flow en % por 8 h (escalada si el periodo de Binance no es de 8 h)."""
    h = f.get("funding_h")
    return f["funding"] * 8 / h if h else f["funding"]


def funding_hours(rows, coins) -> dict[str, float]:
    """Horas del periodo de funding de cada moneda según /fapi/v1/fundingInfo (las que no salen, 8 h)."""
    listed = {}
    for r in rows if isinstance(rows, list) else []:
        try:
            listed[r["symbol"]] = float(r["fundingIntervalHours"])
        except (KeyError, TypeError, ValueError):
            continue
    return {c: h if (h := listed.get(f"{c}USDT")) and h > 0 else FUNDING_H_DEFAULT for c in coins}


def bias_of(ls: float, funding: float, pctl: float | None = None) -> dict:
    """Sesgo del minorista: lado por el ratio de cuentas largo/corto; extremo si está en el 10 % más alto o más bajo
    de los últimos 7 días de esa moneda (pctl) o si el funding se dispara. Sin historial se usan umbrales fijos."""
    side = "long" if ls > 1.6 else "short" if ls < 0.95 else "neutral"
    extreme = funding > 0.028 or (pctl >= 90 or pctl <= 10 if pctl is not None else ls > 2.55 or ls < 0.75)
    label = {"long": "muy largo" if extreme else "largo", "short": "muy corto" if extreme else "corto"}.get(side, "neutral")
    return {"side": side, "extreme": extreme, "label": label, "longPct": ls / (1 + ls) * 100}


def flow_from(ls_row: dict, prem_row: dict, oi_rows: list, taker_row: dict) -> dict:
    ls = float(ls_row["longShortRatio"])
    funding = float(prem_row["lastFundingRate"]) * 100
    oi = (float(oi_rows[-1]["sumOpenInterestValue"]) / float(oi_rows[0]["sumOpenInterestValue"]) - 1) * 100 if len(oi_rows) > 1 else 0.0
    r = float(taker_row["buySellRatio"])
    return {"ls": ls, "funding": funding, "oi": oi, "taker": r / (1 + r)}


class Market:
    def __init__(self, bus: Bus, session: aiohttp.ClientSession):
        self.bus = bus
        self.s = session
        self.prices: dict[str, float] = {}
        self.updated: dict[str, int] = {}   # ms en que llegó el último ticker de cada moneda (no la hora de publicar)
        self.spread: dict[str, float] = {}
        self.flow: dict[str, dict] = {}
        self.labels: dict[str, str] = {}
        self.ls_hist: dict[str, list[float]] = {}   # ratio largo/corto horario de los últimos 7 días
        self.ls_hist_at = 0.0
        self.funding_h: dict[str, float] = {}       # horas del periodo de funding (fundingInfo de Binance)
        self.funding_h_next = 0.0                    # próxima consulta (monotonic)
        self.ok = {"prices": None, "flow": None}
        self.err = {"prices": "", "flow": ""}
        self._dirty = False

    async def run(self) -> None:
        await asyncio.gather(self._prices(), self._flow_loop(), self._push_loop())

    async def _prices(self) -> None:
        wait = 2
        sub = {"op": "subscribe", "args": [{"channel": "tickers", "instId": f"{c}-{config.QUOTE}"} for c in config.COINS]}
        while True:
            try:
                async with self.s.ws_connect(OKX_WS, heartbeat=20) as ws:
                    await ws.send_json(sub)
                    self.ok["prices"], self.err["prices"], wait = True, "", 2
                    log.info("Precios OKX EU conectados")
                    last_ping = time.monotonic()
                    while True:
                        msg = await ws.receive(timeout=25)
                        if time.monotonic() - last_ping > 20:
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
                            raise ConnectionError(d.get("msg", "error de suscripción"))
                        for t in d.get("data", []):
                            c = t["instId"].split("-")[0]
                            self.prices[c] = float(t["last"])
                            self.updated[c] = now_ms()
                            bid, ask = float(t.get("bidPx") or 0), float(t.get("askPx") or 0)
                            if bid and ask:
                                self.spread[c] = (ask - bid) / ((ask + bid) / 2) * 10000
                            self._dirty = True
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.ok["prices"], self.err["prices"] = False, (str(e) or type(e).__name__)[:160]
                log.warning("Precios: %s (reintento en %d s)", self.err["prices"], wait)
                await asyncio.sleep(wait)
                wait = min(wait * 2, 120)

    async def _get(self, url: str):
        async with self.s.get(url) as r:
            r.raise_for_status()
            return await r.json(content_type=None)

    async def _flow_coin(self, c: str) -> dict:
        sym = f"{c}USDT"
        ls, prem, oi, tk, cb = await asyncio.gather(
            self._get(f"{BN}/futures/data/globalLongShortAccountRatio?symbol={sym}&period=5m&limit=1"),
            self._get(f"{BN}/fapi/v1/premiumIndex?symbol={sym}"),
            self._get(f"{BN}/futures/data/openInterestHist?symbol={sym}&period=5m&limit=13"),
            self._get(f"{BN}/futures/data/takerlongshortRatio?symbol={sym}&period=5m&limit=1"),
            self._get(f"https://api.exchange.coinbase.com/products/{c}-USD/ticker"),
        )
        f = flow_from(ls[-1], prem, oi, tk[-1])
        if self.prices.get(c):
            f["prem"] = (float(cb["price"]) / self.prices[c] - 1) * 100
        return f

    async def _refresh_ls_hist(self) -> None:
        if time.monotonic() - self.ls_hist_at < 6 * 3600 and self.ls_hist:
            return
        for c in config.COINS:
            rows = await self._get(f"{BN}/futures/data/globalLongShortAccountRatio?symbol={c}USDT&period=1h&limit=168")
            self.ls_hist[c] = sorted(float(r["longShortRatio"]) for r in rows)
        self.ls_hist_at = time.monotonic()

    async def _refresh_funding_h(self) -> None:
        if time.monotonic() < self.funding_h_next:
            return
        self.funding_h_next = time.monotonic() + 600   # si falla, otro intento en 10 min (con el último dato)
        self.funding_h = funding_hours(await self._get(f"{BN}/fapi/v1/fundingInfo"), config.COINS)
        self.funding_h_next = time.monotonic() + FUNDING_INFO_S

    def _pctl(self, c: str, ls: float) -> float | None:
        h = self.ls_hist.get(c)
        return None if not h else sum(1 for x in h if x <= ls) / len(h) * 100

    def _extremity(self, c: str) -> float:
        """Lo lejos que está el percentil de 7 días del centro; el percentil 0 es el extremo más fuerte, no neutro."""
        p = self.flow[c]["pctl"]
        return 0.0 if p is None else abs(p - 50)

    async def _flow_loop(self) -> None:
        await asyncio.sleep(5)   # deja que lleguen los primeros precios para calcular la prima
        while True:
            try:
                try:
                    await self._refresh_ls_hist()
                except Exception as e:   # sin historial se usan umbrales fijos
                    log.warning("Historial de largos/cortos no disponible: %s", e)
                try:
                    await self._refresh_funding_h()
                except Exception as e:   # sin el dato se supone el periodo de 8 h (o el último conocido)
                    log.warning("Periodo de funding de Binance no disponible: %s", e)
                rows = await asyncio.gather(*(self._flow_coin(c) for c in config.COINS))
                for c, f in zip(config.COINS, rows):
                    f["ts"] = now_ms()   # hora del dato: quien lo lea descarta el viejo (fresh_flow)
                    f["spread"] = self.spread.get(c)
                    f["pctl"] = self._pctl(c, f["ls"])
                    if self.funding_h.get(c):
                        f["funding_h"] = self.funding_h[c]
                    b = bias_of(f["ls"], funding_8h(f), f["pctl"])
                    f["label"], f["extreme"] = b["label"], b["extreme"]
                    self.flow[c] = f
                    if self.labels.get(c) != b["label"]:
                        first = c not in self.labels
                        self.labels[c] = b["label"]
                        self.bus.publish({"type": "event", "kind": "flow", "agent": "flow",
                                          "title": f"{'Lectura inicial: m' if first else 'Cambio: m'}inorista {b['label']} en {c}",
                                          "coins": [c], "text": self._flow_text(f, b)})
                lead = max(config.COINS, key=self._extremity)
                b = bias_of(self.flow[lead]["ls"], funding_8h(self.flow[lead]), self.flow[lead]["pctl"])
                self.bus.publish({"type": "agent", "agent": "flow",
                                  "patch": {"now": f"{lead}: minorista {b['label']} ({b['longPct']:.0f} % de cuentas en largo)"}})
                self.ok["flow"], self.err["flow"] = True, ""
                self._dirty = True
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.ok["flow"], self.err["flow"] = False, (str(e) or type(e).__name__)[:160]
                log.warning("Flujo minorista: %s", self.err["flow"])
            await asyncio.sleep(config.FLOW_INTERVAL_S)

    @staticmethod
    def _flow_text(f: dict, b: dict) -> str:
        n = lambda x, d: f"{x:.{d}f}".replace(".", ",")
        rank = f" · percentil {n(f['pctl'], 0)} de 7 días" if f.get("pctl") is not None else ""
        per = f"{n(f['funding'], 4)} %/{n(f['funding_h'], 0)} h" if f.get("funding_h") else f"{n(f['funding'], 4)} %/8 h"
        return (f"Ratio L/S {n(f['ls'], 2)} ({n(b['longPct'], 0)} % de cuentas en largo){rank} · funding {per} · "
                f"OI {'+' if f['oi'] >= 0 else ''}{n(f['oi'], 1)} % en 1 h · compras agresivas {n(f['taker'] * 100, 0)} %")

    async def _push_loop(self) -> None:
        while True:
            await asyncio.sleep(config.MARKET_PUSH_S)
            if self._dirty and self.prices:
                self._dirty = False
                # ts = llegada del último precio: si el WebSocket cae, el flujo sigue publicando pero ts no avanza.
                self.bus.publish({"type": "market", "ts": max(self.updated.values(), default=now_ms()), "prices": dict(self.prices),
                                  "updated": dict(self.updated), "flow": fresh_flow({"flow": self.flow}),
                                  "spread": dict(self.spread)})
