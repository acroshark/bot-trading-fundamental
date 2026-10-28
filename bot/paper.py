"""Paper trading: broker ficticio con precios reales de OKX EU. Recibe los tickets del estratega (patch.strat), pasa
los controles de riesgo y la aprobación, ejecuta, gestiona los stops y publica la cartera (contratos 2.5, 2.6 y 3.2).
Las operaciones se guardan en la tabla trades y las abiertas se recuperan al reiniciar.
Cada ticket puede traer sus salidas (beR, trailStartR, tp1R + tp1Frac, tp2R; ver exit_rules); sin ellas, como siempre.
Un ticket inmediato con maxAgeMs no se ejecuta si la aprobación (veto o a mano) llega más tarde que eso.
Instrumento del ticket («instrument»): «spot» (por defecto, como siempre) o «perp» (futuro perpetuo: así van los cortos
del motor de aperturas). Con las reglas del usuario, un corto sin «instrument» va en perpetuo (en contado USDC no se
puede ir corto) y un corto al contado explícito se acepta con el aviso de que no es ejecutable en OKX EU.
Un perpetuo cobra o paga el funding en cada liquidación (00:00, 08:00 y 16:00 UTC) con la tasa de
market.flow[moneda].funding o, sin ella, la última conocida; entra en el resultado, en R y en las comisiones. Su
nominal total no pasa de 2 veces el capital (2:1) y en la exposición cuenta su margen (la mitad del nominal).
SUPUESTO del funding (funding_8h): market.flow[moneda].funding es el lastFundingRate de Binance USDT-M × 100, la tasa de
UN periodo de liquidación, y se da por hecho que ese periodo es de 8 h (lo normal en BTC, ETH, SOL y XRP). Si Binance
liquida esa moneda cada 4 h o 1 h, la tasa es por ese periodo y el paper cobraría de menos, salvo que la fila traiga
«funding_h» (fundingIntervalHours de /fapi/v1/fundingInfo; hoy market.py no lo envía), con la que se escala a 8 h.
Tampoco es la tasa de OKX X-Perps EU, donde se ejecutaría de verdad: es una aproximación.
Mandos de la web (server.on_ws_message): decide, close, set_mode, halt / resume (parada manual, persistente) y cancel.
HUECOS (hallazgo n.º 47): una posición abierta que pasa más de GAP_MIN_MS sin gestionarse (el bot parado o caído, o sin
precios) no sigue como si nada. Cada posición guarda la última vez que se gestionó con precio («seen», cada 30 s como
mucho); al volver, se reconstruye el hueco con las velas de 1 min de Binance al contado (fetch_bars, pasadas a la escala de
OKX EU con el cociente entre los dos precios al empezar el hueco): si tocó el stop, el objetivo o el tope de tiempo, se
cierra a ese nivel y a esa hora (replay_gap), con el funding de las liquidaciones de por medio. Mientras tanto no se
gestiona. Sin velas tras GAP_TRIES intentos, sigue en vivo y queda marcada («huecos» con completo = False)."""
import asyncio
import hashlib
import json
import logging
import random
import time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from . import config, regions
from .bus import Bus
from .market import fresh_flow
from .models import now_ms
from .store import Store

log = logging.getLogger("paper")
MAD = ZoneInfo("Europe/Madrid")
MODES = ("auto", "veto", "manual")

MAX_SPREAD_BPS = 5.0
SPREAD_FALLBACK_BPS = 2.0      # sin dato de diferencial, al ejecutar se supone este
MAX_NOTIONAL_PCT = 40.0        # nominal máximo de una operación, en % del capital
MAX_EXPOSURE_PCT = 100.0
MACRO_PAUSE_MS = 10 * 60_000   # sin entradas si hay un dato macro de alto impacto en este plazo
MANUAL_MS = 10 * 60_000        # plazo para aprobar a mano
RANGE_MS = 5 * 60_000          # rango de los 5 primeros minutos de la ventana
EXPIRE_MS = 30 * 60_000        # una entrada programada sin ruptura caduca a los 30 min
HOLD_MS = 4 * 3600_000         # cierre por tiempo si el ticket no trae el fin de la ventana
MANAGE_STEP_R = 0.25           # el stop dinámico se anuncia cada vez que avanza 0,25 R
SAVE_EVERY_MS = 30_000         # las posiciones abiertas se guardan al menos con esta frecuencia
PRICE_MAX_AGE_MS = 15_000      # un precio más viejo no sirve para entrar, mover stops ni cerrar
STEP_EVERY_S = 1.0
PORTFOLIO_EVERY_S = 2.0
ERR_LOG_EVERY_S = 300          # un fallo que se repite cada ronda se registra como mucho cada 5 min
CLOSED_IN_PORTFOLIO = 50
DAY_MAX_ENTRIES = 6            # reglas del usuario: entradas por día de Madrid como mucho, de TODOS los orígenes
DAY_MAX_RISK_PCT = 3.0         # ... y riesgo comprometido en stop (0,5 % × 6); con un preset más estricto, el suyo
GAP_MIN_MS = 2 * 60_000        # una posición sin gestionar más tiempo que esto (bot parado o sin precios) se reconstruye
GAP_RETRY_S = 60               # reintento de las velas del hueco
GAP_TRIES = 10                 # sin velas completas tras tantos intentos, se reconstruye con lo que haya y sigue en vivo
GAP_HOLE_MS = 5 * 60_000       # velas del hueco con un salto mayor que esto: incompletas
COST_SLIP_BPS = 2.0            # deslizamiento supuesto por lado en el coste de ida y vuelta (como el backtest); es
                               # también el máximo del simulado, que se suma al medio diferencial
BE_MARGIN_BPS = 2.0            # margen del break-even sobre el coste de ida y vuelta
STOP_NAMES = {"inicial": "Stop de pérdida", "be": "Stop en break-even", "dinamico": "Stop dinámico"}
MODE_TEXT = {"auto": "Aprobación automática (solo paper)", "veto": "Ventana de veto de {s} s",
             "manual": "Apruebas cada orden (caduca en 10 min)"}
INSTRUMENTS = ("spot", "perp")
PERP_MAX_LEVERAGE = 2.0        # nominal en perpetuos ≤ 2 × capital (máximo para particulares en España)
FUNDING_EVERY_MS = 8 * 3600_000   # liquidaciones del funding: 00:00, 08:00 y 16:00 UTC
HALT_WHY = "Paper detenido por ti"   # parada manual desde la web (no es la pausa por pérdida diaria)
CAPS = ("halt", "cancel")            # mandos de la web que admite este paper (portfolio.caps)
# Campos sin los que una operación guardada no se puede cargar (los que escribe _fill / _close).
NEED_OPEN = ("id", "story", "coin", "side", "qty", "entry", "sl", "sl_initial", "sl_kind", "sl_pub", "stop_pct",
             "trail_pct", "high_water", "opened", "riskUsd", "notional", "fee_open", "tp2", "tp2R", "be", "hold_until",
             "hold_label", "headline", "strategy", "window", "market", "exec")
NEED_CLOSED = ("id", "story", "opened", "closed", "pnl")


# ---------------------------------------------------------------- utilidades

def _f(x, default=None):
    try:
        return float(x) if x not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _n(x: float, d: int = 2) -> str:
    """Número para los textos: coma decimal y punto de miles a partir de 10.000 (como es-ES)."""
    x = round(float(x), d)
    s = f"{abs(x):,.{d}f}" if abs(x) >= 10000 else f"{abs(x):.{d}f}"
    s = s.replace(",", "_").replace(".", ",").replace("_", ".")
    return ("-" if x < 0 else "") + s


def _sg(x: float, d: int = 2) -> str:
    return ("+" if round(x, d) >= 0 else "") + _n(x, d)


def _px(x: float) -> str:
    return _n(x, 2 if abs(x) >= 100 else 4)


def _qty(coin: str, q: float) -> str:
    return _n(q, 4 if coin == "BTC" else 3 if coin == "ETH" else 1)


def _verb(side: str) -> str:
    return "Compra" if side == "long" else "Venta"


def _hm(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, MAD).strftime("%H:%M")


def _ago(ms: float) -> str:
    s = max(0, int(ms // 1000))
    return f"{s} s" if s < 120 else f"{s // 60} min"


def _mode_text(mode: str) -> str:
    return MODE_TEXT[mode].format(s=config.VETO_SECONDS)


def _day(ms: int):
    return datetime.fromtimestamp(ms / 1000, MAD).date()


def _midnight(day) -> int:
    return int(datetime.combine(day, dtime(0), tzinfo=MAD).timestamp() * 1000)


def day_start_ms(ms: int) -> int:
    """Medianoche de Madrid del día de ms (el día de trading del paper empieza ahí)."""
    return _midnight(_day(ms))


def next_midnight_ms(ms: int) -> int:
    return _midnight(_day(ms) + timedelta(days=1))


def instrument_of(t: dict | None) -> str:
    """Instrumento de un ticket: «perp» solo si lo pide; sin el campo, contado (como siempre)."""
    return "perp" if (t or {}).get("instrument") == "perp" else "spot"


def next_funding_ms(ms: int) -> int:
    """Primera liquidación del funding estrictamente después de ms."""
    return (int(ms) // FUNDING_EVERY_MS + 1) * FUNDING_EVERY_MS


def funding_8h(row: dict | None) -> float | None:
    """Tasa de funding en % por 8 h de una fila de market.flow (None sin dato). «funding» es la tasa de un periodo de
    Binance USDT-M; se supone de 8 h salvo que la fila traiga «funding_h» (horas del periodo), con la que se escala."""
    r, h = _f((row or {}).get("funding")), _f((row or {}).get("funding_h"))
    return r * 8 / h if r is not None and h and h > 0 else r


def funding_pay(side: str, notional: float, rate_pct: float) -> float:
    """Lo que cobra (+) o paga (−) un perpetuo en una liquidación, en USDC: con tasa positiva los largos pagan y los
    cortos cobran (tasa en % del nominal por periodo de 8 h, como market.flow[moneda].funding)."""
    return -(1 if side == "long" else -1) * notional * rate_pct / 100


def market_of(window: str) -> str | None:
    """Bolsa a la que se refiere una ventana ("Apertura Londres" → "londres")."""
    w = (window or "").lower()
    for m in regions.MARKETS.values():
        if m.key in w.replace(" ", "") or m.name.lower() in w:
            return m.key
    return None


# ---------------------------------------------------------------- salidas (funciones puras, también para el backtest)

def fee_pct(inst: str | None = "spot") -> float:
    """Comisión por lado en % (taker) del instrumento: la de los perpetuos (config.FEE_PERP_PCT) o la del contado
    (config.FEE_PCT). Por defecto las dos son 0,10 % (n.º 56 y 60: ver config)."""
    return config.FEE_PERP_PCT if inst == "perp" else config.FEE_PCT


def cost_rt_pct(inst: str | None = "spot") -> float:
    """Coste de ida y vuelta en % del nominal: comisión y deslizamiento en cada lado (0,24 % con 0,10 % y 2 pb)."""
    return 2 * (fee_pct(inst) + COST_SLIP_BPS / 100)


def stop_loss_frac(side: str, stop_pct: float, exit_slip_bps: float, inst: str | None = "spot") -> float:
    """Pérdida al saltar el stop inicial, por unidad de nominal: el recorrido hasta el stop, el deslizamiento al salir y
    las comisiones de entrada y salida. Un hueco que salte más allá del stop no se puede prever."""
    d = 1 if side == "long" else -1
    k = (1 - d * stop_pct / 100) * (1 - d * exit_slip_bps / 10000)   # precio de salida / precio de entrada
    return abs(1 - k) + fee_pct(inst) / 100 * (1 + k)


def be_offset_pct() -> float:
    """Distancia del break-even a la entrada: coste de ida y vuelta + 2 pb (no la entrada pelada)."""
    return cost_rt_pct() + BE_MARGIN_BPS / 100


def exit_rules(t: dict) -> dict:
    """Salidas de un ticket. Un ticket sin estos campos se gestiona como siempre (break-even a +1 R en la entrada y stop
    dinámico desde el principio).
    - beR: R desde el que el stop pasa a break-even; null o ≤ 0 = nunca. Con el campo, el nivel cubre costes.
    - trailStartR: R desde el que actúa el stop dinámico (null = desde la entrada).
    - tp1R + tp1Frac: salida parcial de esa fracción en tp1 (0 = sin parcial). tp2R: objetivo final (null = sin tope)."""
    trail = _f(t.get("trailPct"))
    if "beR" in t:
        be_r = _f(t.get("beR"))
        be_r, be_pct = (be_r if be_r and be_r > 0 else None), be_offset_pct()
    else:
        be_r, be_pct = 1.0, 0.0
    ts, tp1R, tp2R = _f(t.get("trailStartR")), _f(t.get("tp1R")), _f(t.get("tp2R"))
    tp1R = tp1R if tp1R and tp1R > 0 else None
    frac = min(1.0, max(0.0, _f(t.get("tp1Frac"), 0.0))) if tp1R else 0.0
    if config.USER_RULES:   # horquilla pura: o el objetivo o el stop, sin break-even, stop dinámico ni parciales
        trail, be_r, ts, frac = None, None, None, 0.0
    return {"trail": trail if trail and trail > 0 else None, "be_r": be_r, "be_pct": be_pct,
            "trail_start_r": ts if ts and ts > 0 else None, "tp1R": tp1R, "tp1_frac": frac,
            "tp2R": tp2R if tp2R and tp2R > 0 else None}


def stop_update(d: int, entry: float, stop_pct: float, trail: float | None, best: float, stop: float, be: bool,
                be_r: float | None = 1.0, be_pct: float = 0.0,
                trail_start_r: float | None = None) -> tuple[float, bool, str | None]:
    """Stop tras un nuevo extremo favorable `best` (nunca retrocede). Devuelve (stop, be, tipo del movimiento o None).
    - Break-even a +be_r R (None = nunca) al nivel entrada ± be_pct %, cuando el precio ya lo ha superado.
    - Stop dinámico desde el extremo a max(trail, stop_pct − ganancia %), activo desde +trail_start_r R."""
    kind = None
    r = entry * stop_pct / 100
    move = d * (best - entry)
    if not be and be_r is not None and move >= be_r * r - 1e-12:
        lvl = entry * (1 + d * be_pct / 100)
        if d * (best - lvl) > 0:
            be = True
            if d * (lvl - stop) > 0:
                stop, kind = lvl, "be"
    if trail and (trail_start_r is None or move >= trail_start_r * r - 1e-12):
        dist = max(trail, stop_pct - move / entry * 100)
        ts = best * (1 - d * dist / 100)
        if d * (ts - stop) > entry * 1e-9:
            stop, kind = ts, "dinamico"
    return stop, be, kind


def replay_gap(side: str, sl: float, tp2: float | None, hold_until: float | None, bars, factor: float = 1.0) -> dict:
    """Lo que pasó con una posición abierta durante un hueco (bot parado o sin precios), con velas de 1 min
    [t, o, h, l, c, …] ordenadas (las de Binance en USDT, pasadas a la escala de OKX EU multiplicando por factor). Primera
    salida, como en el paper: el tope de tiempo cumplido al empezar la vela (sale a su apertura); una apertura que ya salta
    el stop (sale a la apertura, peor que el stop) o el objetivo (sale en el objetivo, como la orden límite); dentro de la
    vela, el stop o el objetivo y, si toca los dos, el stop (el peor caso: no se sabe cuál fue antes). Horquilla pura (las
    reglas del usuario): un stop dinámico o un break-even no se reconstruyen. Devuelve {salida: None | "stop" | "objetivo"
    | "tiempo", px (nivel de salida, sin deslizamiento), at (ms), best (extremo favorable), last (último cierre), n (velas
    miradas)}."""
    d = 1 if side == "long" else -1
    out = {"salida": None, "px": None, "at": None, "best": None, "last": None, "n": 0}
    for r in bars:
        t, o, h, l, c = int(r[0]), float(r[1]) * factor, float(r[2]) * factor, float(r[3]) * factor, float(r[4]) * factor
        if hold_until and t >= hold_until:
            out.update(salida="tiempo", px=o, at=max(int(hold_until), t))
            return out
        out["n"] += 1
        fav, adv = (h, l) if d > 0 else (l, h)
        if d * (o - sl) <= 0:
            out.update(salida="stop", px=o, at=t)
            return out
        if tp2 and d * (o - tp2) >= 0:
            out.update(salida="objetivo", px=tp2, at=t)
            return out
        if d * (adv - sl) <= 0:
            out.update(salida="stop", px=sl, at=t)
            return out
        out["best"] = fav if out["best"] is None or d * (fav - out["best"]) > 0 else out["best"]
        if tp2 and d * (fav - tp2) >= 0:
            out.update(salida="objetivo", px=tp2, at=t)
            return out
        out["last"] = c
    return out


def day_caps(motor: dict | None = None) -> tuple[int, float]:
    """(entradas, riesgo en %) del día de Madrid para todo el paper: los del preset en marcha del motor de aperturas
    (max_day_entries y max_day_risk_pct; E2 da 4 y 2 %, «6 diarias» 6 y 3 %) y nunca más de DAY_MAX_ENTRIES y
    DAY_MAX_RISK_PCT. Sin dato, esos."""
    m = motor if isinstance(motor, dict) else {}
    n, r = _f(m.get("max_day_entries")), _f(m.get("max_day_risk_pct"))
    return ((min(DAY_MAX_ENTRIES, int(n)) if n and n > 0 else DAY_MAX_ENTRIES),
            (min(DAY_MAX_RISK_PCT, r) if r and r > 0 else DAY_MAX_RISK_PCT))


def gap_complete(bars, a: int, b: int) -> bool:
    """¿Las velas cubren el hueco [a, b) sin saltos de más de GAP_HOLE_MS?"""
    ts = [int(r[0]) for r in bars or [] if a <= int(r[0]) < b]
    if b - a < 60_000:
        return True
    if not ts or ts[0] > a + 2 * 60_000 or ts[-1] + 60_000 < b - 3 * 60_000:
        return False
    return all(y - x <= GAP_HOLE_MS for x, y in zip(ts, ts[1:]))


# ---------------------------------------------------------------- broker

class PaperBroker:
    def __init__(self, bus: Bus, store: Store, get_market, get_orderflow=None, clock=now_ms, slip_bps=None,
                 fetch_bars=None, day_limits=None):
        self.bus = bus
        self.store = store
        self.get_market = get_market                          # () -> último "market" (precios y diferencial OKX EU)
        self.get_orderflow = get_orderflow or (lambda: None)  # () -> último "orderflow" (diferencial del libro)
        self.clock = clock                                    # () -> ms; inyectable en las pruebas
        self.slip_bps = slip_bps or (lambda: random.uniform(0.5, 2.0))
        # async (moneda, desde_ms, hasta_ms) -> velas de 1 min [t, o, h, l, c, …] de Binance, para los huecos (None: no
        # se reconstruyen, como antes)
        self.fetch_bars = fetch_bars
        self.day_limits = day_limits           # () -> parámetros del motor en marcha (sus topes del día), o None
        self._gaps: dict[str, dict] = {}       # posiciones con un hueco por reconstruir {desde, intentos, proximo}
        self.mode = config.APPROVAL_MODE if config.APPROVAL_MODE in MODES else "veto"
        if self.mode != config.APPROVAL_MODE:
            log.warning("Modo de aprobación desconocido «%s»: se usa veto", config.APPROVAL_MODE)
        self.start_equity = float(config.PAPER_EQUITY)
        self.cuenta: dict | None = None        # cartera de prueba con nombre {nombre, eur, eur_usdc, usdc, desde, ficticia}
        self.positions: dict[str, dict] = {}   # abiertas, por noticia
        self.pending: dict[str, dict] = {}     # esperando aprobación
        self.scheduled: dict[str, dict] = {}   # aprobadas, esperando su ventana
        self.closed: list[dict] = []           # cerradas (formato 3.2), de la más antigua a la más reciente
        self.halt_until = 0                    # sin operaciones nuevas hasta esta hora (pérdida máxima del día)
        self.halted: dict | None = None        # parada manual desde la web {at, why}; dura hasta resume
        self.day_base: dict = {}               # capital a precio de mercado en la primera ronda del día {date, equity}
        self._live_day = None                  # último día de Madrid en que ha corrido el broker
        self._pf_key = None
        self._saved_at = 0
        self._err_at: dict[str, float] = {}    # último registro de cada fallo repetido
        self._stale_seen: list[str] = []       # monedas con posición abierta y sin precio actualizado
        store.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS trades(id TEXT PRIMARY KEY, story TEXT, opened INTEGER, closed INTEGER, body TEXT);
            CREATE INDEX IF NOT EXISTS trades_closed ON trades(closed);
            CREATE TABLE IF NOT EXISTS paper_state(key TEXT PRIMARY KEY, body TEXT);
            """
        )
        self._load()

    # ---- persistencia

    def _load(self) -> None:
        # Un registro ilegible o incompleto se deja en la base y se ignora: no debe tumbar el arranque ni cada ronda.
        for tid, body, closed in self.store.db.execute("SELECT id, body, closed FROM trades ORDER BY opened"):
            try:
                d = json.loads(body)
                missing = [k for k in (NEED_OPEN if closed is None else NEED_CLOSED) if k not in d]
                if missing:
                    raise KeyError("faltan " + ", ".join(missing))
            except (ValueError, TypeError, KeyError) as e:
                log.error("Paper: operación guardada %s ilegible, se ignora: %s", tid, e)
                continue
            if closed is None:
                self.positions[d["story"]] = d
            else:
                self.closed.append(d)
        self.closed.sort(key=lambda t: t["closed"])
        if any(_f(p.get("seen")) is None for p in self.positions.values()):
            # guardadas antes de «seen»: el hueco empieza en la última actividad del bot (el último evento guardado)
            try:
                last = self.store.db.execute("SELECT MAX(ts) FROM events").fetchone()[0]
            except Exception:
                last = None
            for p in self.positions.values():
                if _f(p.get("seen")) is None:
                    p["seen"] = max(int(p["opened"]), int(last or 0))
        q = self._get_state("queue")
        q = q if isinstance(q, dict) else {}
        now = self.clock()
        for it in q.get("pending", []):
            if self._queue_ok(it, ("deadline", "mode")):
                it["late"] = now >= it["deadline"]   # caducó con el bot parado: no se aprueba sola
                self.pending[it["story"]] = it
        for it in q.get("scheduled", []):
            if self._queue_ok(it, ("at", "expires")):
                it["rng"] = None                     # el rango se vuelve a medir con precios nuevos
                self.scheduled[it["story"]] = it
        self.halt_until = int(_f(self._get_state("halt"), 0))
        h = self._get_state("manual_halt")
        self.halted = h if isinstance(h, dict) and _f(h.get("at")) else None
        if self.halted:
            log.warning("Paper: sigue detenido por ti desde %s: sin operaciones nuevas hasta reanudar",
                        datetime.fromtimestamp(_f(self.halted["at"]) / 1000, MAD).strftime("%d-%m %H:%M"))
        base = self._get_state("day_start")
        self.day_base = base if isinstance(base, dict) and _f(base.get("equity")) is not None else {}
        self._load_settings()
        if self.positions or self.pending or self.scheduled:
            log.info("Paper: recuperadas %d posiciones, %d pendientes y %d programadas",
                     len(self.positions), len(self.pending), len(self.scheduled))

    @staticmethod
    def _queue_ok(it, keys: tuple) -> bool:
        t = it.get("ticket") if isinstance(it, dict) else None
        ok = isinstance(t, dict) and all(k in it for k in ("story", "notional", *keys)) and all(
            k in t for k in ("coin", "side", "timing", "risk", "stopPct"))
        if not ok:
            log.error("Paper: entrada de la cola ilegible, se ignora: %s", str(it)[:160])
        return ok

    def _load_settings(self) -> None:
        """Modo de aprobación y capital inicial guardados: un reinicio no los cambia sin avisar."""
        saved = self._get_state("mode")
        saved = saved if isinstance(saved, dict) else {}
        if saved.get("mode") in MODES and saved.get("config") == config.APPROVAL_MODE:
            self.mode = saved["mode"]   # el último cambio hecho desde la web sigue valiendo
        elif saved.get("mode") in MODES:   # la configuración ha cambiado después: manda ella y queda guardada
            if saved["mode"] != self.mode:
                log.warning("Paper: BOT_APPROVAL_MODE ha cambiado desde que elegiste «%s» en la web: se usa «%s»",
                            saved["mode"], self.mode)
            self._set_state("mode", {"mode": self.mode, "config": config.APPROVAL_MODE})
        cuenta = self._get_state("cuenta")   # la deja scripts/nueva_cartera_paper.py: su capital manda sobre BOT_PAPER_EQUITY
        self.cuenta = cuenta if isinstance(cuenta, dict) and (_f(cuenta.get("usdc")) or 0) > 0 else None
        if self.cuenta:
            self.start_equity = float(self.cuenta["usdc"])
        start = _f(self._get_state("start_equity"))
        if start and (self.closed or self.positions):   # con operaciones hechas, el capital inicial ya no se mueve
            if abs(start - self.start_equity) > 1e-9:
                log.warning("Paper: el capital inicial guardado (%s) no coincide con BOT_PAPER_EQUITY (%s): se mantiene "
                            "el guardado para no reescribir el historial", start, self.start_equity)
            self.start_equity = start
        elif start != self.start_equity:
            self._set_state("start_equity", self.start_equity)

    def _get_state(self, key: str):
        row = self.store.db.execute("SELECT body FROM paper_state WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except ValueError:
            log.error("Paper: estado guardado «%s» ilegible, se ignora", key)
            return None

    def _set_state(self, key: str, value) -> None:
        self.store.db.execute("INSERT OR REPLACE INTO paper_state VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))
        self.store.db.commit()

    def _save_queue(self) -> None:
        self._set_state("queue", {"pending": list(self.pending.values()), "scheduled": list(self.scheduled.values())})

    def _save_trade(self, body: dict, closed: int | None = None) -> None:
        self.store.db.execute("INSERT OR REPLACE INTO trades VALUES (?,?,?,?,?)",
                              (body["id"], body["story"], body["opened"], closed, json.dumps(body, ensure_ascii=False)))
        self.store.db.commit()

    # ---- mercado

    def _market(self) -> dict:
        try:
            return self.get_market() or {}
        except Exception as e:
            log.warning("Paper: sin datos de mercado: %s", e)
            return {}

    def _prices(self, now: int | None = None) -> dict:
        """Precios utilizables de OKX EU. Si «market» trae la hora de cada precio (updated: {moneda: ms}), se descartan
        los de más de 15 s: con el WebSocket caído, market se reenvía con los precios viejos."""
        m = self._market()
        now = self.clock() if now is None else now
        upd = m.get("updated")
        out = {}
        for c, p in (m.get("prices") or {}).items():
            p = _f(p)
            t = _f((upd or {}).get(c))
            if p and (upd is None or (t is not None and now - t <= PRICE_MAX_AGE_MS)):
                out[c] = p
        return out

    def _price_issue(self, coin: str, now: int, prices: dict) -> str | None:
        """Por qué no hay precio utilizable de coin (None si lo hay)."""
        if prices.get(coin) is not None:
            return None
        m = self._market()
        t = _f((m.get("updated") or {}).get(coin))
        if _f((m.get("prices") or {}).get(coin)) and t is not None:
            return f"Precio de {coin} sin actualizar desde hace {_ago(now - t)}"
        return f"Sin precio de {coin} en OKX EU"

    def _spread(self, coin: str) -> float | None:
        """Diferencial en pb: el del ticker de OKX EU y, si falta, el del libro (orderflow)."""
        sp = (self._market().get("spread") or {}).get(coin)
        if sp is None:
            try:
                sp = (((self.get_orderflow() or {}).get("coins") or {}).get(coin) or {}).get("spread_bps")
            except Exception:
                sp = None
        return float(sp) if sp is not None else None

    def _funding_rate(self, coin: str) -> float | None:
        """Tasa de funding de ahora en % por 8 h (funding_8h de market.flow[moneda]) o None sin dato."""
        return funding_8h(fresh_flow(self._market(), self.clock()).get(coin))

    def _macro_soon(self, now: int) -> dict | None:
        for e in (self.bus.state.get("calendar") or {}).get("events") or []:
            if e.get("impact") == "alto" and now <= (e.get("t") or 0) <= now + MACRO_PAUSE_MS:
                return e
        return None

    def _news_score(self, story: str) -> dict | None:
        """Notas del triaje de la noticia (2.7) para la tabla de resultados."""
        try:
            row = self.store.db.execute("SELECT body FROM events WHERE story=? AND kind IN ('triaged','lowprio') "
                                        "ORDER BY ts DESC LIMIT 1", (story,)).fetchone()
        except Exception:
            return None
        if not row:
            return None
        tr = (json.loads(row[0]).get("patch") or {}).get("triage") or {}
        return {"importancia": tr.get("importancia"), "credibilidad": tr.get("credibilidad")}

    # ---- contabilidad

    @staticmethod
    def _dir(side: str) -> int:
        return 1 if side == "long" else -1

    def _upnl(self, p: dict, px: float | None = None) -> float:
        """Resultado latente: lo que queda abierto, la comisión de entrada, lo ya cobrado en salidas parciales y el
        funding liquidado (perpetuos)."""
        px = px or p.get("last") or p["entry"]
        return (self._dir(p["side"]) * (px - p["entry"]) * p["qty"] - p["fee_open"] + p.get("realized", 0.0)
                + p.get("funding", 0.0))

    @staticmethod
    def _open_notional(p: dict) -> float:
        """Nominal que sigue abierto (tras una salida parcial, la parte que queda)."""
        return p["notional"] * p["qty"] / (p.get("qty0") or p["qty"])

    @staticmethod
    def _weight(inst: str) -> float:
        """Parte del nominal que cuenta en la exposición: entera al contado, el margen (1/2) en un perpetuo a 2:1."""
        return 1 / PERP_MAX_LEVERAGE if inst == "perp" else 1.0

    def _perp_notional(self, queued: bool = True) -> float:
        """Nominal en perpetuos abierto y, con queued, el comprometido (pendientes y programadas)."""
        out = sum(self._open_notional(p) for p in self.positions.values() if p.get("instrument") == "perp")
        if queued:
            out += sum(i["notional"] for i in [*self.pending.values(), *self.scheduled.values()]
                       if instrument_of(i["ticket"]) == "perp")
        return out

    def equity(self, prices: dict | None = None) -> float:
        """Capital a precio de mercado: inicial + cerradas + abiertas (ya descontada la comisión de entrada)."""
        prices = self._prices() if prices is None else prices
        return (self.start_equity + sum(t["pnl"] for t in self.closed)
                + sum(self._upnl(p, prices.get(p["coin"])) for p in self.positions.values()))

    def _day_start_equity(self, now: int) -> float:
        """Capital con que empezó el día de Madrid: la foto a precio de mercado de la primera ronda tras la medianoche.
        Sin foto (el bot no estaba en marcha a medianoche) se usa lo realizado hasta entonces."""
        if self.day_base.get("date") == _day(now).isoformat():
            return float(self.day_base["equity"])
        cut = day_start_ms(now)
        return self.start_equity + sum(t["pnl"] for t in self.closed if t["closed"] < cut)

    def _roll_day(self, now: int, prices: dict) -> None:
        """En la primera ronda tras la medianoche de Madrid fija el capital de partida del día, con las posiciones que
        pasan la noche a precio de mercado: así su resultado de ayer no cuenta en la pérdida de hoy."""
        today = _day(now).isoformat()
        if self.day_base.get("date") != today and self._live_day and self._live_day < today:
            self.day_base = {"date": today, "equity": round(self.equity(prices), 4)}
            self._set_state("day_start", self.day_base)
        self._live_day = today

    def day_pnl(self, now: int, prices: dict | None = None) -> tuple[float, float]:
        """Resultado del día de Madrid en USDC y en % del capital con que empezó el día."""
        base = self._day_start_equity(now)
        pnl = self.equity(prices) - base
        return pnl, (pnl / base * 100 if base else 0.0)

    def _exit_slip_bps(self, coin: str) -> float:
        """Deslizamiento previsto al salir: medio diferencial (el de ahora o el supuesto) y el máximo del simulado."""
        sp = self._spread(coin)
        return (SPREAD_FALLBACK_BPS if sp is None else sp) / 2 + COST_SLIP_BPS

    def _notional(self, eq: float, t: dict) -> float:
        """Tamaño por riesgo: la pérdida en el stop inicial, con las comisiones de entrada y salida y el deslizamiento
        previsto, no pasa del riesgo por operación. Tope del 40 % del capital."""
        risk, stop = _f(t.get("risk"), 0.0), _f(t.get("stopPct"), 0.0)
        if stop <= 0 or risk <= 0 or eq <= 0:
            return 0.0
        loss = stop_loss_frac(t.get("side"), stop, self._exit_slip_bps(t.get("coin")), instrument_of(t))
        return min(eq * risk / 100 / loss, eq * MAX_NOTIONAL_PCT / 100)

    def _committed(self) -> float:
        """Exposición abierta y comprometida (los perpetuos, por su margen)."""
        return (sum(self._open_notional(p) * self._weight(p.get("instrument")) for p in self.positions.values())
                + sum(i["notional"] * self._weight(instrument_of(i["ticket"]))
                      for i in list(self.pending.values()) + list(self.scheduled.values())))

    def _check_halt(self, now: int, prices: dict) -> None:
        """Al superar la pérdida máxima del día no entran operaciones nuevas hasta la medianoche de Madrid."""
        self._roll_day(now, prices)
        if self.halt_until and now >= self.halt_until:
            self.halt_until = 0
            self._set_state("halt", 0)
            self._status("risk", "Nuevo día en Madrid: acaba la pausa por pérdida diaria (el paper sigue detenido por ti)"
                         if self.halted else "Nuevo día en Madrid: se vuelven a aceptar operaciones")
        if self.halt_until:
            return
        loss = max(0.0, -self.day_pnl(now, prices)[1])
        if loss >= config.MAX_DAILY_LOSS_PCT:
            self.halt_until = next_midnight_ms(now)
            self._set_state("halt", self.halt_until)
            log.warning("Paper: pérdida del día %.2f %% ≥ %.2f %%: sin operaciones nuevas hasta la medianoche",
                        loss, config.MAX_DAILY_LOSS_PCT)
            self._status("risk", f"Pérdida del día {_n(loss)} % ≥ {_n(config.MAX_DAILY_LOSS_PCT)} %: "
                                 "sin operaciones nuevas hasta la medianoche de Madrid")

    # ---- publicación

    def _event(self, kind: str, agent: str, story: str, coin: str | None, title: str, text: str, patch: dict,
               now: int, **extra) -> dict:
        return self.bus.publish({"type": "event", "ts": now, "kind": kind, "agent": agent, "story": story, "title": title,
                                 "text": text, "coins": [coin] if coin else [], "patch": patch, **extra})

    def _status(self, agent: str, text: str) -> None:
        self.bus.publish({"type": "agent", "agent": agent, "patch": {"now": text}})

    def _exec_text(self) -> str:
        n = len(self.positions)
        s = f"{n} {'posición abierta' if n == 1 else 'posiciones abiertas'} · capital {_n(self.equity())} {config.QUOTE} (paper)"
        return f"{HALT_WHY}: solo se gestionan las abiertas (stops y objetivos) · {s}" if self.halted else s

    def _exec_status(self) -> None:
        self._status("exec", self._exec_text())

    def _risk_text(self) -> str:
        if self.halted:
            return f"{HALT_WHY}: no entran operaciones nuevas hasta que lo reanudes"
        if self.halt_until:
            return "Operativa detenida por la pérdida máxima del día hasta la medianoche de Madrid"
        return (f"Límites: {_n(config.RISK_PER_TRADE_PCT)} % por operación (en el stop, con costes), "
                f"{_n(config.MAX_DAILY_LOSS_PCT)} % de pérdida al día, {config.MAX_POSITIONS} posiciones")

    def _human_text(self) -> str:
        t = _mode_text(self.mode)
        return f"{HALT_WHY}: no se aprueba ni se ejecuta nada nuevo · {t}" if self.halted else t

    def announce(self) -> None:
        """Enciende en la web los agentes de riesgo, aprobación y ejecución."""
        notes = {"risk": self._risk_text(), "human": self._human_text(),
                 "exec": self._exec_text() if self.halted
                 else f"Paper trading con precios reales de OKX EU · capital {_n(self.equity())} {config.QUOTE}"}
        for agent, text in notes.items():
            self.bus.publish({"type": "agent", "agent": agent, "patch": {"off": False, "note": None, "now": text}})

    def _after_command(self) -> None:
        """Tras un mando de la web, la cartera sale al momento: la web no espera a la ronda de 2 s para verlo."""
        try:
            self.publish_portfolio(force=True)
        except Exception:
            self._log_err("portfolio", "Paper trading: fallo al publicar la cartera")

    def set_mode(self, mode: str) -> bool:
        """Cambia el modo de aprobación y lo guarda: tras reiniciar sigue valiendo (salvo que cambie BOT_APPROVAL_MODE).
        Al pasar a manual, lo que esperaba el plazo de veto pasa a esperar tu aprobación: ya no se aprueba solo."""
        if mode not in MODES:
            return False
        now = self.clock()
        self.mode = mode
        self._set_state("mode", {"mode": mode, "config": config.APPROVAL_MODE})
        moved = [it for it in self.pending.values() if mode == "manual" and it["mode"] == "veto" and not it.get("late")]
        for it in moved:
            it.update(mode="manual", deadline=max(it["deadline"], now + MANUAL_MS))
            self._event("humanwait", "human", it["story"], it["ticket"]["coin"], self._title(it),
                        f"Has pasado a aprobar cada orden: ya no se aprueba sola (caduca a las {_hm(it['deadline'])})",
                        {"human": {"mode": "manual", "deadline": it["deadline"]}}, now)
        if moved:
            self._save_queue()
        self._status("human", self._human_text())
        self._after_command()
        return True

    def halt(self, why: str = "Detenido por ti") -> bool:
        """Parada manual, guardada para después de un reinicio: no entran órdenes nuevas ni se ejecutan las pendientes
        ni las programadas; las posiciones abiertas se siguen gestionando (stops, objetivos y cierre por tiempo) para no
        dejar riesgo sin control. Devuelve False si ya estaba detenido."""
        done = not self.halted
        if done:
            self.halted = {"at": self.clock(), "why": why}
            self._set_state("manual_halt", self.halted)
            log.warning("Paper: %s (%s): sin operaciones nuevas hasta reanudar", HALT_WHY.lower(), why)
        self._status("risk", self._risk_text())
        self._status("human", self._human_text())
        self._exec_status()
        self._after_command()
        return done

    def resume(self, why: str = "Reanudado por ti") -> bool:
        """Quita la parada manual (la pausa por pérdida diaria, si la hay, sigue hasta la medianoche).
        Devuelve False si no estaba detenido."""
        done = bool(self.halted)
        if done:
            self.halted = None
            self._set_state("manual_halt", None)
            log.warning("Paper: reanudado (%s)", why)
        self._status("risk", self._risk_text())
        self._status("human", self._human_text())
        self._exec_status()
        self._after_command()
        return done

    def cancel(self, story: str, why: str = "Cancelada por ti") -> bool:
        """Cancela una orden que espera tu aprobación (humanko) o su ventana (blocked). No toca las posiciones abiertas:
        para salir de una está close. Devuelve False si no había nada que cancelar."""
        now = self.clock()
        if story in self.pending:
            done = self._resolve(story, False, why, now)
        elif story in self.scheduled:
            self._block(self.scheduled[story], why, now)
            done = True
        else:
            done = False
        if done:
            log.info("Paper: %s cancelada (%s)", story, why)
        else:
            log.info("Paper: nada que cancelar en %s%s", story,
                     " (está abierta: para salir, ciérrala)" if story in self.positions else "")
        self._after_command()
        return done

    # ---- entrada: riesgo y aprobación

    def submit(self, ticket: dict, story: str, headline: str = "", strategy: str = "", window: str = "") -> bool:
        """Recibe un ticket (formato patch.strat). Devuelve True si pasa los controles de riesgo."""
        now = self.clock()
        t = dict(ticket or {})
        coin, side = t.get("coin"), t.get("side")
        window = window or t.get("win") or ""
        strategy = strategy or t.get("mode") or ""
        note = None
        if config.USER_RULES and side == "short":   # en contado USDC (OKX EU) no se puede ir corto
            if t.get("instrument") is None:   # p. ej. los cortos de la fase 2: van en perpetuo, con funding y 2:1
                t["instrument"] = "perp"
                note = "Corto sin instrumento: va en perpetuo (en contado USDC no se puede ir corto)"
            elif t.get("instrument") == "spot":
                note = "Corto al contado: no se puede ejecutar en OKX EU (USDC), solo se mide en paper"
        prices = self._prices(now)
        self._check_halt(now, prices)
        checks, notional = self._risk_checks(t, story, now, prices)
        if note:
            checks.append({"ok": True, "t": note})
        title = f"{_verb(side)} {coin} · {t.get('mode') or strategy}"
        bad = next((c for c in checks if not c["ok"]), None)
        if bad:
            self._event("riskko", "risk", story, coin, title, f"Vetada: {bad['t']}",
                        {"risk": {"checks": checks, "ok": False, "why": bad["t"]}}, now)
            log.info("Paper: %s vetada por riesgo: %s", story, bad["t"])
            return False
        self._event("riskok", "risk", story, coin, title, f"{len(checks)} de {len(checks)} controles superados",
                    {"risk": {"checks": checks, "ok": True}}, now)
        item = {"story": story, "ticket": t, "headline": headline, "strategy": strategy, "window": window,
                "market": t.get("market") or market_of(window), "notional": round(notional, 2),
                "news_score": self._news_score(story), "submitted": now}
        self._approve(item, now)
        return True

    def _invalid(self, t: dict, story: str, risk: float, stop: float) -> str | None:
        if t.get("none"):
            return "El estratega no propone operación"
        if t.get("coin") not in config.COINS:
            return f"Moneda no operable: {t.get('coin')}"
        if t.get("side") not in ("long", "short"):
            return f"Lado desconocido: {t.get('side')}"
        if t.get("instrument") not in (None, *INSTRUMENTS):
            return f"Instrumento desconocido: {t.get('instrument')} (spot o perp)"
        if not 0 < stop <= 20:
            return f"Stop no válido: {_n(stop)} %"
        if risk <= 0:
            return "Riesgo por operación no válido"
        if config.USER_RULES:
            if stop < config.RULE_MIN_STOP_PCT - 1e-9:
                return f"Regla del usuario: stop de {_n(stop)} % por debajo del mínimo de {_n(config.RULE_MIN_STOP_PCT)} %"
            rr = _f(t.get("tp2R"))
            if not rr or rr < config.RULE_MIN_RR - 1e-9:
                return (f"Regla del usuario: objetivo de {_n(rr)} R por debajo del mínimo de {_n(config.RULE_MIN_RR)} R"
                        if rr else "Regla del usuario: sin objetivo fijo (hace falta al menos 2R)")
            net = stop * rr - cost_rt_pct(instrument_of(t))   # objetivo bruto menos el coste de ida y vuelta
            if net < config.RULE_MIN_NET_PCT - 1e-9:
                return (f"Regla del usuario: el objetivo (+{_n(stop * rr)} %) deja {_n(net)} % neto de costes, por debajo "
                        f"del {_n(config.RULE_MIN_NET_PCT)} %")
        if t.get("timing") not in ("now", "window"):
            return f"Momento de entrada desconocido: {t.get('timing')}"
        if t.get("timing") == "window" and not _f(t.get("winAt")):
            return "Entrada programada sin hora (winAt)"
        if story in self.positions or story in self.pending or story in self.scheduled:
            return "Ya hay una operación en curso para esta noticia"
        return None

    def day_entries(self, now: int, eq: float | None = None) -> dict[str, float]:
        """Entradas del día de Madrid de now, de todos los orígenes, con su riesgo en stop en % del capital: abiertas y
        cerradas que se abrieron hoy (riesgo real) y pendientes o programadas (el riesgo de su ticket). Como
        apertura.paper_day_entries, pero con el estado del propio paper."""
        eq = self.equity() if eq is None else eq
        start = day_start_ms(now)
        out: dict[str, float] = {}
        for x in [*self.positions.values(), *self.closed]:
            if _f(x.get("opened"), 0) >= start:
                r = _f(x.get("riskUsd"))
                out[x["story"]] = round(r / eq * 100, 4) if r and eq else config.RISK_PER_TRADE_PCT
        for it in [*self.pending.values(), *self.scheduled.values()]:
            out.setdefault(it["story"], _f(it["ticket"].get("risk"), config.RISK_PER_TRADE_PCT))
        return out

    def _day_check(self, story: str, risk: float, now: int, eq: float) -> dict:
        """Topes del día (reglas del usuario): entradas y riesgo del día de Madrid de todos los orígenes (motor, entrada
        por noticia y fase 2), con los del preset en marcha del motor (day_caps)."""
        try:
            motor = self.day_limits() if self.day_limits is not None else None
        except Exception:
            motor = None
        n_max, r_max = day_caps(motor)
        ent = self.day_entries(now, eq)
        ent.pop(story, None)
        used = sum(ent.values())
        if len(ent) >= n_max:
            return {"ok": False, "t": f"Entradas del día: {len(ent)} de {n_max} (todas las del paper, día de Madrid): "
                                      "no caben más"}
        if used + risk > r_max + 1e-9:
            return {"ok": False, "t": f"Riesgo del día {_n(used)} % + {_n(risk)} % > {_n(r_max)} % (todas las entradas "
                                      "del paper, día de Madrid)"}
        return {"ok": True, "t": f"Entradas del día {len(ent)} de {n_max} y riesgo {_n(used)} % + {_n(risk)} % ≤ "
                                 f"{_n(r_max)} % (día de Madrid)"}

    def _risk_checks(self, t: dict, story: str, now: int, prices: dict) -> tuple[list[dict], float]:
        coin = t.get("coin")
        risk, stop = _f(t.get("risk"), 0.0), _f(t.get("stopPct"), 0.0)
        bad = self._invalid(t, story, risk, stop)
        eq = self.equity(prices)
        notional = self._notional(eq, t) if not bad else 0.0
        loss = max(0.0, -self.day_pnl(now, prices)[1])
        n = len(self.positions) + len(self.pending) + len(self.scheduled)
        inst = instrument_of(t)
        expo = self._committed() + notional * self._weight(inst)
        expo_pct = expo / eq * 100 if eq else 0.0
        now_t = t.get("timing") == "now"
        max_risk, max_loss, max_pos = config.RISK_PER_TRADE_PCT, config.MAX_DAILY_LOSS_PCT, config.MAX_POSITIONS
        side = "largo" if t.get("side") == "long" else "corto"

        sp = self._spread(coin)
        noprice = self._price_issue(coin, now, prices) if now_t else None
        if noprice:
            spread = {"ok": False, "t": noprice}
        elif sp is None:
            spread = {"ok": True, "t": f"Diferencial de {coin}: sin dato ahora (se comprueba al ejecutar)"}
        elif not now_t:   # entrada programada: como la pausa macro, cuenta el diferencial del momento de entrar
            spread = {"ok": True, "t": f"Diferencial de {coin}: se comprueba al ejecutar (ahora {_n(sp, 1)} pb)"}
        else:
            ok = sp <= MAX_SPREAD_BPS
            spread = {"ok": ok, "t": f"Diferencial de {coin}: {_n(sp, 1)} pb {'≤' if ok else '>'} {_n(MAX_SPREAD_BPS, 0)} pb"}

        macro = self._macro_soon(now) if now_t else None
        if not now_t:
            macro_t = "Pausa macro: no aplica a una entrada programada (se vuelve a mirar al ejecutar)"
        elif macro:
            macro_t = f"Dato macro de alto impacto en {max(0, round((macro['t'] - now) / 60_000))} min: {macro.get('name', '')}"
        else:
            macro_t = "Sin dato macro de alto impacto en los próximos 10 min"

        # Cada texto dice lo que ha pasado: «≤» si se cumple y «>» si no (llega al usuario como motivo del veto).
        risk_ok = risk <= max_risk
        loss_ok = not self.halt_until and loss < max_loss
        pos_ok = n < max_pos
        expo_ok = expo <= eq * MAX_EXPOSURE_PCT / 100 + 1e-6
        if self.halt_until:
            loss_t = f"Operativa detenida hasta la medianoche de Madrid: pérdida del día {_n(loss)} % (máximo {_n(max_loss)} %)"
        else:
            loss_t = f"Pérdida del día {_n(loss)} % {'<' if loss_ok else '≥'} {_n(max_loss)} % como máximo"
        expo_d = 1 if round(expo_pct) == round(MAX_EXPOSURE_PCT) else 0   # junto al límite, con un decimal
        # Una sola operación por moneda: el motor de aperturas y la fase 2 no pueden doblar el riesgo ni ir a la contra.
        same = next((s for s, x in [*self.positions.items(), *self.pending.items(), *self.scheduled.items()]
                     if s != story and (x.get("coin") or (x.get("ticket") or {}).get("coin")) == coin), None)
        checks = [
            {"ok": not bad, "t": bad or f"Ticket válido: {coin} en {side} con stop de {_n(stop)} %"},
            {"ok": risk_ok, "t": f"Riesgo por operación {_n(risk)} % "
                                 + (f"≤ {_n(max_risk)} % del capital (en el stop, con comisiones y deslizamiento)"
                                    if risk_ok else f"> {_n(max_risk)} % permitido")},
            {"ok": loss_ok, "t": loss_t},
            {"ok": pos_ok, "t": f"Posiciones abiertas o en curso {n} de {max_pos}"
                                + (" como máximo" if pos_ok else ": no caben más")},
            {"ok": expo_ok, "t": f"Exposición total {_n(expo_pct, expo_d)} % {'≤' if expo_ok else '>'} "
                                 f"{_n(MAX_EXPOSURE_PCT, 0)} % del capital"
                                 + (" (los perpetuos, por su margen: la mitad del nominal)"
                                    if inst == "perp" or self._perp_notional() else "")},
            spread,
            {"ok": macro is None, "t": macro_t},
            {"ok": same is None, "t": f"Sin otra operación abierta o en curso en {coin}" if same is None
                else f"Ya hay una operación en {coin} ({same}): una sola por moneda"},
        ]
        if config.USER_RULES:   # límites diarios de entradas y riesgo, para todos los orígenes (también la fase 2)
            checks.append(self._day_check(story, risk, now, eq))
        if inst == "perp":   # apalancamiento máximo para particulares en España
            perp = self._perp_notional() + notional
            ok = perp <= eq * PERP_MAX_LEVERAGE + 1e-6
            checks.append({"ok": ok, "t": f"Nominal en perpetuos {_n(perp / eq * 100 if eq else 0.0, 0)} % "
                                          f"{'≤' if ok else '>'} {_n(PERP_MAX_LEVERAGE * 100, 0)} % del capital (2:1, "
                                          "máximo para particulares en España)"})
        if self.halted:   # parada manual: es el motivo del veto, delante de todo lo demás
            checks.insert(0, {"ok": False, "t": HALT_WHY})
        return checks, notional

    @staticmethod
    def _title(item: dict) -> str:
        t = item["ticket"]
        return f"{_verb(t['side'])} {t['coin']} · {t.get('mode') or item['strategy']}"

    def _approve(self, item: dict, now: int) -> None:
        t, story = item["ticket"], item["story"]
        title = self._title(item)
        if self.mode == "auto":
            self._event("humanok", "human", story, t["coin"], title, "Aprobación automática (solo permitido en paper)",
                        {"human": {"mode": "auto", "by": "Automático (paper)", "at": now}}, now)
            self._execute(item, now)
            return
        wait = config.VETO_SECONDS * 1000 if self.mode == "veto" else MANUAL_MS
        item.update(mode=self.mode, deadline=now + wait)
        self.pending[story] = item
        self._save_queue()
        text = (f"Se aprobará sola en {config.VETO_SECONDS} s si no la vetas" if self.mode == "veto"
                else f"Esperando tu decisión (caduca a las {_hm(now + wait)})")
        self._event("humanwait", "human", story, t["coin"], title, text,
                    {"human": {"mode": self.mode, "deadline": now + wait}}, now)

    def decide(self, story: str, ok: bool, by: str | None = None) -> bool:
        """Tu decisión desde la web (o la de un agente). Devuelve False si no había nada pendiente.
        Con el paper detenido, una aprobación no ejecuta nada (blocked) y una entrada programada espera."""
        done = self._resolve(story, bool(ok), by or ("Aprobada por ti" if ok else "Rechazada por ti"), self.clock())
        if done:
            self._after_command()
        return done

    def _resolve(self, story: str, ok: bool, by: str, now: int) -> bool:
        item = self.pending.pop(story, None)
        if item is None:
            return False
        self._save_queue()
        t = item["ticket"]
        title = self._title(item)
        human = {"mode": item["mode"], "by": by, "at": now}
        if ok:
            self._event("humanok", "human", story, t["coin"], title, by, {"human": human}, now)
            self._execute(item, now)
        else:
            self._event("humanko", "human", story, t["coin"], title, by, {"human": {**human, "no": True}}, now)
        return True

    # ---- ejecución

    def _execute(self, item: dict, now: int) -> None:
        t, story, coin = item["ticket"], item["story"], item["ticket"]["coin"]
        if t["timing"] == "window":
            at = int(_f(t["winAt"]))
            item.update(at=at, expires=at + EXPIRE_MS, rng=None, wait=None,
                        exec={"scheduled": at, "notional": item["notional"]})
            self.scheduled[story] = item
            self._save_queue()
            self._event("scheduled", "exec", story, coin, f"{_verb(t['side'])} {coin} en {item['window'] or 'su ventana'}",
                        f"Se activa a las {_hm(at)} (Madrid) si rompe el rango de los 5 primeros minutos; "
                        f"caduca a las {_hm(at + EXPIRE_MS)}", {"exec": dict(item["exec"])}, now)
            return
        if self.halted:   # aprobada con el paper detenido: no entra (una programada, en cambio, espera en su ventana)
            self._block(item, HALT_WHY, now)
            return
        # Un ticket con maxAgeMs (motor de aperturas) no se ejecuta si se aprobó más tarde: su plan ya es viejo.
        age, max_age = now - int(_f(item.get("submitted"), now)), _f(t.get("maxAgeMs"))
        if max_age and age > max_age:
            self._block(item, f"Aprobada {_ago(age)} después de enviarla (máximo {_ago(max_age)}): la señal y el plan "
                              "de salidas ya son viejos", now)
            return
        prices = self._prices(now)
        hard, soft = self._blockers(item, now, prices)
        if hard or soft:
            self._block(item, hard or soft, now)
            return
        self._fill(item, now, prices)

    def _blockers(self, item: dict, now: int, prices: dict) -> tuple[str | None, str | None]:
        """Lo que impide entrar ahora: (definitivo, pasajero). Lo pasajero solo hace esperar a una entrada programada."""
        t = item["ticket"]
        coin = t["coin"]
        self._check_halt(now, prices)
        eq = self.equity(prices)
        hard = soft = None
        if self.halt_until:
            hard = "Operativa detenida por la pérdida máxima del día hasta la medianoche de Madrid"
        elif len(self.positions) >= config.MAX_POSITIONS:
            hard = f"Ya hay {len(self.positions)} posiciones abiertas (máximo {config.MAX_POSITIONS})"
        else:
            inst, new = instrument_of(t), self._notional(eq, t)
            expo = (sum(self._open_notional(p) * self._weight(p.get("instrument")) for p in self.positions.values())
                    + new * self._weight(inst))
            perp = self._perp_notional(queued=False) + new
            if expo > eq * MAX_EXPOSURE_PCT / 100 + 1e-6:
                hard = f"La exposición subiría al {_n(expo / eq * 100, 0)} % del capital (máximo {_n(MAX_EXPOSURE_PCT, 0)} %)"
            elif inst == "perp" and perp > eq * PERP_MAX_LEVERAGE + 1e-6:
                hard = (f"El nominal en perpetuos subiría al {_n(perp / eq * 100, 0)} % del capital (máximo "
                        f"{_n(PERP_MAX_LEVERAGE * 100, 0)} %, 2:1)")
        sp, macro = self._spread(coin), self._macro_soon(now)
        noprice = self._price_issue(coin, now, prices)
        if self.halted:   # parada manual: una programada espera (por si la reanudas antes de que caduque)
            soft = HALT_WHY
        elif noprice:
            soft = noprice
        elif sp is not None and sp > MAX_SPREAD_BPS:
            soft = f"Diferencial de {coin} {_n(sp, 1)} pb > {_n(MAX_SPREAD_BPS, 0)} pb"
        elif macro:
            soft = f"Dato macro de alto impacto en {max(0, round((macro['t'] - now) / 60_000))} min: {macro.get('name', '')}"
        return hard, soft

    def _block(self, item: dict, why: str, now: int) -> None:
        if self.scheduled.pop(item["story"], None) is not None:
            self._save_queue()
        t = item["ticket"]
        self._event("blocked", "exec", item["story"], t["coin"], f"{_verb(t['side'])} {t['coin']}", why,
                    {"exec": {**(item.get("exec") or {}), "blocked": True, "why": why}}, now)
        log.info("Paper: %s bloqueada: %s", item["story"], why)

    def _step_scheduled(self, it: dict, now: int, prices: dict) -> None:
        """Entrada programada: mide el rango de los 5 primeros minutos (hasta winAt) y entra si lo rompe a favor."""
        t = it["ticket"]
        coin, d = t["coin"], self._dir(t["side"])
        if now >= it["expires"]:
            if self.halted:
                why = f"{HALT_WHY}: la entrada programada caduca sin ejecutarse"
            else:
                why = "Sin ruptura del rango de los 5 primeros minutos en 30 min: la orden caduca"
                if it.get("wait"):
                    why += f" (lo último que la frenó: {it['wait']})"
            self._block(it, why, now)
            return
        px = prices.get(coin)
        if px is None:
            if now >= it["at"]:   # sin precio utilizable no se mide el rango; queda anotado por si caduca
                it["wait"] = self._price_issue(coin, now, prices)
            return
        if now < it["at"] - RANGE_MS:
            return
        rng = it.get("rng")
        if not rng:
            rng = it["rng"] = {"first": now, "hi": px, "lo": px}
        if now < max(it["at"], rng["first"] + RANGE_MS):   # si el bot llegó tarde, el rango se mide 5 min desde entonces
            rng["hi"], rng["lo"] = max(rng["hi"], px), min(rng["lo"], px)
            return
        if not (px > rng["hi"] if d > 0 else px < rng["lo"]):
            return
        hard, soft = self._blockers(it, now, prices)
        if hard:
            self._block(it, hard, now)
            return
        if soft:
            it["wait"] = soft
            return
        del self.scheduled[it["story"]]
        self._save_queue()
        it["exec"] = {**(it.get("exec") or {}), "range": {"hi": rng["hi"], "lo": rng["lo"]}}
        self._fill(it, now, prices)

    @staticmethod
    def _hold(t: dict, now: int) -> tuple[int, str]:
        """Cierre por tiempo: fin de la ventana (winEnd, con holdLabel como nombre si lo trae: p. ej. los 7 días de
        seguridad del modo impulso), duración (holdMin) o 4 h."""
        end = _f(t.get("winEnd"))
        if end and end > now:
            return int(end), str(t.get("holdLabel") or "fin de la ventana")
        mins = _f(t.get("holdMin"))
        if mins and mins > 0:
            return now + int(mins * 60_000), f"{_n(mins, 0)} min"
        return now + HOLD_MS, f"{HOLD_MS // 3600_000} h"

    def _fill(self, item: dict, now: int, prices: dict) -> None:
        """Orden a mercado al precio de OKX EU más medio diferencial y un deslizamiento de 0,5 a 2 pb."""
        t0 = time.perf_counter()
        t, story = item["ticket"], item["story"]
        coin, side = t["coin"], t["side"]
        d = self._dir(side)
        stop = _f(t["stopPct"])
        rules = exit_rules(t)
        trail = rules["trail"]
        ref = prices[coin]
        eq = self.equity(prices)
        notional = self._notional(eq, t)
        inst = instrument_of(t)
        stop_loss = notional * stop_loss_frac(side, stop, self._exit_slip_bps(coin), inst)   # pérdida prevista en el stop
        ex = {**(item.get("exec") or {}), "sent": now}
        self._event("order", "exec", story, coin, f"{_verb(side)} {_qty(coin, notional / ref)} {coin}",
                    f"{t.get('entryType') or 'A mercado'} · referencia {_px(ref)} {config.QUOTE} · "
                    f"stop {_px(ref * (1 - d * stop / 100))}", {"exec": dict(ex)}, now)

        sp = self._spread(coin)
        slip = (SPREAD_FALLBACK_BPS if sp is None else sp) / 2 + self.slip_bps()
        px = ref * (1 + d * slip / 10000)
        qty = notional / px
        r = px * stop / 100
        tp1R, tp2R = _f(t.get("tp1R")), rules["tp2R"]
        hold_until, hold_label = self._hold(t, now)
        sl = px - d * r
        pos = {"id": "t" + hashlib.sha1(f"{story}|{now}".encode()).hexdigest()[:12], "story": story, "coin": coin, "side": side,
               "qty": qty, "entry": px, "sl": sl, "sl_initial": sl, "sl_kind": "inicial", "sl_pub": sl, "stop_pct": stop,
               "trail_pct": trail, "high_water": px, "last": px, "opened": now, "seen": now,
               "riskUsd": notional * stop / 100,
               "notional": notional, "fee_open": notional * fee_pct(inst) / 100,
               "tp1": px + d * r * tp1R if tp1R else None, "tp2": px + d * r * tp2R if tp2R else None, "tp2R": tp2R,
               "be": False, "hold_until": hold_until, "hold_label": hold_label, "headline": item["headline"],
               "strategy": item["strategy"], "window": item["window"], "market": item["market"],
               "news_score": item.get("news_score"), "equity_open": eq,
               # Salidas del ticket (exit_rules) y salidas parciales ya hechas.
               "qty0": qty, "tp1R": rules["tp1R"], "tp1_frac": rules["tp1_frac"], "tp1_done": False,
               "be_r": rules["be_r"], "be_pct": rules["be_pct"], "trail_start_r": rules["trail_start_r"],
               "realized": 0.0, "fees_exit": 0.0, "partials": []}
        if isinstance(t.get("tags"), dict):   # etiquetas del motor (noticia, apertura anterior…) para Resultados
            pos["tags"] = dict(t["tags"])
        pos["instrument"] = inst
        if inst == "perp":   # funding: cobrado (+) o pagado (−) en cada liquidación mientras siga abierta
            pos.update(funding=0.0, funding_n=0, funding_next=next_funding_ms(now),
                       funding_rate=self._funding_rate(coin))
        ex.update(px=px, slip=round(slip, 3), ms=round((time.perf_counter() - t0) * 1000, 1), notional=round(notional, 2),
                  qty=qty, sl=sl, tp1=pos["tp1"], tp2=pos["tp2"], trail=trail, tp1Frac=rules["tp1_frac"],
                  beR=rules["be_r"], trailStartR=rules["trail_start_r"], stopLossUsd=round(stop_loss, 2),
                  instrument=inst)
        pos["exec"] = ex
        self.positions[story] = pos
        self._save_trade(pos)
        plan = ""
        if pos["tp1_frac"]:
            plan += f" · parcial del {_n(pos['tp1_frac'] * 100, 0)} % en {_px(pos['tp1'])}"
        if pos["tp2"]:
            plan += f" · objetivo {_px(pos['tp2'])}"
        if pos["be_r"] is None:
            plan += " · sin break-even"
        elif pos["be_pct"]:
            plan += f" · break-even a +{_n(pos['be_r'], 1)} R cubriendo costes"
        if trail and pos["trail_start_r"]:
            plan += f" · stop dinámico desde +{_n(pos['trail_start_r'], 1)} R"
        if inst == "perp":
            fr = pos["funding_rate"]
            plan += (f" · perpetuo: funding cada 8 h (ahora {_n(fr, 4)} %)" if fr is not None
                     else " · perpetuo: funding cada 8 h (sin tasa aún)")
        self._event("filled", "exec", story, coin, f"{'Comprado' if d > 0 else 'Vendido'} {_qty(coin, qty)} {coin} a {_px(px)}",
                    f"Deslizamiento {_n(slip, 1)} pb · latencia {_n(ex['ms'], 0)} ms · nominal {_n(notional, 0)} {config.QUOTE}"
                    f" · riesgo {_n(pos['riskUsd'])} {config.QUOTE} ({_n(stop_loss)} con costes) · stop {_px(sl)}" + plan,
                    {"exec": dict(ex)}, now)
        log.info("Paper: %s %s %.6f a %.4f (%s)", side, coin, qty, px, story)
        self._exec_status()

    # ---- gestión de la posición

    def _manage(self, p: dict, px: float, now: int) -> None:
        d, coin = self._dir(p["side"]), p["coin"]
        p["last"] = px
        if d * (px - p["high_water"]) > 0:
            p["high_water"] = px
        r = p["entry"] * p["stop_pct"] / 100
        # Break-even y stop dinámico (stop_update). La distancia del dinámico empieza en la del stop inicial y se acorta
        # lo que el precio avanza a favor, hasta trailPct: con uno menor que stopPct no manda desde la entrada.
        was_be = p["be"]
        sl, p["be"], kind = stop_update(d, p["entry"], p["stop_pct"], p["trail_pct"], p["high_water"], p["sl"], p["be"],
                                        p.get("be_r", 1.0), p.get("be_pct", 0.0), p.get("trail_start_r"))
        be_now = p["be"] and not was_be
        if kind:
            p["sl"], p["sl_kind"] = sl, kind
        if be_now or abs(p["sl"] - p["sl_pub"]) >= MANAGE_STEP_R * r:
            p["sl_pub"] = p["sl"]
            p["exec"] = {**p["exec"], "sl": p["sl"], "be": p["be"], "high_water": p["high_water"]}
            lock = d * (p["sl"] - p["entry"]) / r
            if be_now:
                title = f"{coin}: stop a break-even"
                text = (f"+{_n(p.get('be_r') or 1.0, 1)} R alcanzado: stop movido a {_px(p['sl'])} (asegura {_sg(lock)} R); "
                        + ("cubre las comisiones y el deslizamiento supuesto" if p.get("be_pct")
                           else "la operación ya no puede perder salvo comisiones"))
            else:
                gain = d * (p["high_water"] - p["entry"]) / p["entry"] * 100
                dist = max(p["trail_pct"] or 0.0, p["stop_pct"] - gain)
                title = f"{coin}: stop dinámico a {_px(p['sl'])}"
                text = (f"{_n(dist)} % desde el {'máximo' if d > 0 else 'mínimo'} de {_px(p['high_water'])} · "
                        f"asegura {_sg(lock)} R")
            self._event("manage", "exec", p["story"], coin, title, text, {"exec": dict(p["exec"])}, now)
            self._save_trade(p)

        if p.get("tp1_frac") and not p.get("tp1_done") and p.get("tp1") and d * (px - p["tp1"]) >= 0:
            if self._partial(p, now):   # parcial del 100 %: ya está cerrada
                return
        why = exit_px = None
        if p["tp2"] and d * (px - p["tp2"]) >= 0:
            why, exit_px = f"Objetivo ({_n(p['tp2R'], 1)} R)", p["tp2"]   # orden límite: sale al precio del objetivo
        elif d * (px - p["sl"]) <= 0:
            why, exit_px = STOP_NAMES[p["sl_kind"]], self._exit_px(p, px)
        elif now >= p["hold_until"]:
            why, exit_px = f"Cierre por tiempo ({p['hold_label']})", self._exit_px(p, px)
        if why:
            self._close(p, exit_px, why, now)

    def _funding(self, p: dict, px: float, now: int) -> None:
        """Perpetuo: en cada liquidación pasada desde la última, cobra o paga el funding sobre el nominal a precio de
        ahora, con la tasa de market.flow o, sin ella, la última conocida (sin ninguna, esa liquidación no se cobra)."""
        rate = self._funding_rate(p["coin"])
        if rate is not None:
            p["funding_rate"] = rate
        nxt = p.get("funding_next") or next_funding_ms(p["opened"])
        while now >= nxt:
            self._pay_funding(p, nxt, px, now)
            nxt = p["funding_next"]

    def _pay_funding(self, p: dict, at: int, px: float, now: int) -> None:
        """Una liquidación del funding (la de las at) sobre el nominal a precio px, con la última tasa conocida."""
        p["funding_next"] = at + FUNDING_EVERY_MS
        r, notional = p.get("funding_rate"), p["qty"] * px
        if r is None:
            log.warning("Paper: %s sin tasa de funding a las %s: esa liquidación no se cuenta", p["story"], _hm(at))
            return
        amt = funding_pay(p["side"], notional, r)
        p["funding"] = p.get("funding", 0.0) + amt
        p["funding_n"] = p.get("funding_n", 0) + 1
        p["exec"] = {**p["exec"], "funding": round(p["funding"], 4), "funding_n": p["funding_n"]}
        self._event("manage", "exec", p["story"], p["coin"],
                    f"{p['coin']}: funding {'cobrado' if amt >= 0 else 'pagado'} {_sg(amt)} {config.QUOTE} (paper)",
                    f"Liquidación de las {_hm(at)} (Madrid) · tasa {_n(r, 4)} % por 8 h sobre {_n(notional, 0)} "
                    f"{config.QUOTE} de nominal · acumulado {_sg(p['funding'])} {config.QUOTE}",
                    {"exec": dict(p["exec"])}, now)
        self._save_trade(p)

    def _exit_px(self, p: dict, px: float) -> float:
        sp = self._spread(p["coin"])
        slip = (SPREAD_FALLBACK_BPS if sp is None else sp) / 2 + self.slip_bps()
        return px * (1 - self._dir(p["side"]) * slip / 10000)

    def _partial(self, p: dict, now: int) -> bool:
        """Salida parcial de tp1Frac de la posición inicial en tp1 (orden límite: al precio de tp1).
        Devuelve True si la parte era toda la posición (entonces se cierra como objetivo)."""
        d, coin, px = self._dir(p["side"]), p["coin"], p["tp1"]
        q = min(p["qty"], p["qty0"] * p["tp1_frac"])
        if q >= p["qty"] * (1 - 1e-9):
            self._close(p, px, f"Objetivo ({_n(p.get('tp1R') or 0.0, 1)} R)", now)
            return True
        fee = q * px * fee_pct(p.get("instrument")) / 100
        net = d * (px - p["entry"]) * q - fee
        p["qty"] -= q
        p["realized"] = p.get("realized", 0.0) + net
        p["fees_exit"] = p.get("fees_exit", 0.0) + fee
        part = {"qty": q, "px": px, "at": now, "pnl": round(net, 4), "fee": round(fee, 4),
                "why": f"Objetivo parcial ({_n(p.get('tp1R') or 0.0, 1)} R)"}
        p["partials"] = [*p.get("partials", []), part]
        p["tp1_done"] = True
        p["exec"] = {**p["exec"], "qty": p["qty"], "tp1_done": True, "partial": part}
        self._event("manage", "exec", p["story"], coin, f"{coin}: salida parcial en {_px(px)}",
                    f"{'Vendidos' if d > 0 else 'Comprados'} {_qty(coin, q)} {coin} ({_n(p['tp1_frac'] * 100, 0)} % de la "
                    f"posición) a {_px(px)}: {_sg(net)} {config.QUOTE} con comisión · quedan {_qty(coin, p['qty'])} {coin}",
                    {"exec": dict(p["exec"])}, now)
        self._save_trade(p)
        return False

    def _close(self, p: dict, exit_px: float, why: str, now: int, at: int | None = None) -> dict:
        """Cierra lo que queda. pnl, comisiones y R incluyen las salidas parciales; qty es la inicial y exit, el precio
        medio de todas las salidas (así pnl = ±(exit − entry) × qty − comisiones también con parciales). at: la hora de la
        salida si fue antes (un hueco reconstruido); el evento sale ahora."""
        at = now if at is None else int(at)
        d, qty = self._dir(p["side"]), p["qty"]
        parts = p.get("partials") or []
        comm = p["fee_open"] + p.get("fees_exit", 0.0) + qty * exit_px * fee_pct(p.get("instrument")) / 100
        fund = p.get("funding", 0.0)
        fees = comm - fund   # el funding pagado suma a los costes y el cobrado resta (contrato 3.2)
        q_all = qty + sum(x["qty"] for x in parts)
        avg_exit = (exit_px * qty + sum(x["px"] * x["qty"] for x in parts)) / q_all if q_all else exit_px
        pnl = d * (avg_exit - p["entry"]) * q_all - fees
        R = pnl / p["riskUsd"] if p["riskUsd"] else 0.0
        if parts:
            why += " tras salida parcial"
        trade = {"id": p["id"], "story": p["story"], "strategy": p["strategy"], "window": p["window"], "market": p["market"],
                 "coin": p["coin"], "side": p["side"], "qty": round(q_all, 8), "entry": round(p["entry"], 8),
                 "exit": round(avg_exit, 8), "sl_initial": round(p["sl_initial"], 8), "trail_pct": p["trail_pct"],
                 "opened": p["opened"], "closed": at, "reason": why, "pnl": round(pnl, 4),
                 "pnl_pct": round(pnl / p["notional"] * 100, 4), "R": round(R, 4), "fees": round(fees, 4),
                 "headline": p["headline"], "news_score": p.get("news_score"),
                 # Añadidos: riesgo y nominal, resultado en % del capital, stop final y extremo favorable.
                 "riskUsd": round(p["riskUsd"], 4), "notional": round(p["notional"], 2),
                 "equity_pct": round(pnl / (p.get("equity_open") or self.start_equity) * 100, 4),
                 "sl_final": round(p["sl"], 8), "high_water": round(p["high_water"], 8)}
        if parts:   # detalle de las salidas: las parciales y la última (el resto de la posición)
            trade["partials"] = parts
            trade["exit_last"] = round(exit_px, 8)
        if p.get("tags"):
            trade["tags"] = p["tags"]
        for k in ("hueco", "huecos"):   # reconstruida con las velas de un hueco (hallazgo n.º 47)
            if p.get(k):
                trade[k] = p[k]
        trade.update(instrument=p.get("instrument") or "spot", funding=round(fund, 4), commissions=round(comm, 4))
        del self.positions[p["story"]]
        self._gaps.pop(p["story"], None)
        self.closed.append(trade)
        if len(self.closed) > 1 and self.closed[-2]["closed"] > at:
            self.closed.sort(key=lambda t: t["closed"])
        self._save_trade(trade, at)
        result = {"coin": trade["coin"], "side": trade["side"], "qty": trade["qty"], "entry": trade["entry"],
                  "exit": trade["exit"], "why": why, "pnl": trade["pnl"], "pnl_pct": trade["pnl_pct"], "R": trade["R"],
                  "fees": trade["fees"], "opened": trade["opened"], "closedAt": at, "riskUsd": trade["riskUsd"],
                  "notional": trade["notional"], "headline": trade["headline"], "strategy": trade["strategy"],
                  "window": trade["window"]}
        fund_t = (f" (con funding {_sg(fund)} {config.QUOTE} de {p.get('funding_n', 0)} liquidaciones)"
                  if p.get("instrument") == "perp" else "")
        self._event("closed", "exec", p["story"], p["coin"], f"{p['coin']} cerrada: {_sg(pnl)} {config.QUOTE}",
                    f"{why} · {_sg(R)} R · comisiones {_n(fees)} {config.QUOTE}{fund_t}", {"result": result}, now,
                    tone="good" if pnl >= 0 else "bad")
        log.info("Paper: cerrada %s %s (%s): %.2f %s", p["coin"], p["story"], why, pnl, config.QUOTE)
        self._exec_status()
        return trade

    def close(self, story: str, why: str = "Cierre manual") -> bool:
        """Cierra a mercado una posición abierta. Devuelve False si no está abierta o si no hay precio actualizado
        (no se inventa la salida con un precio viejo)."""
        p = self.positions.get(story)
        if not p:
            return False
        now = self.clock()
        prices = self._prices(now)
        if p["coin"] not in prices:
            issue = self._price_issue(p["coin"], now, prices)
            self._status("exec", f"No se puede cerrar {p['coin']} ahora: {issue}")
            log.warning("Paper: cierre de %s aplazado: %s", story, issue)
            return False
        self._close(p, self._exit_px(p, prices[p["coin"]]), why, now)
        self._after_command()
        return True

    # ---- ronda periódica

    def step(self, now: int | None = None) -> None:
        """Una ronda: plazos de aprobación, entradas programadas, stops, límite diario y guardado.
        Cada registro va aparte: si uno falla, se salta y el resto se sigue gestionando."""
        now = self.clock() if now is None else now
        prices = self._prices(now)
        self._roll_day(now, prices)
        for story, it in list(self.pending.items()):
            self._guard(story, self._step_pending, story, it, now)
        for story, it in list(self.scheduled.items()):
            self._guard(story, self._step_scheduled, it, now, prices)
        for story, p in list(self.positions.items()):
            self._guard(story, self._step_position, p, now, prices)
        self._note_stale(prices)
        self._check_halt(now, prices)
        if now - self._saved_at >= SAVE_EVERY_MS:
            self._saved_at = now
            for story, p in list(self.positions.items()):
                self._guard(story, self._save_trade, p)

    def _step_pending(self, story: str, it: dict, now: int) -> None:
        if now < it["deadline"]:
            return
        veto = it["mode"] == "veto" and self.mode != "manual"   # en manual nada se aprueba solo
        if it.get("late"):
            self._resolve(story, False, "Caducada con el bot parado", now)
        elif veto and self.halted:
            self._resolve(story, False, f"{HALT_WHY}: no se aprueba sola", now)
        elif veto:
            self._resolve(story, True, f"Sin veto en {config.VETO_SECONDS} s: aprobada", now)
        else:
            self._resolve(story, False, "Caducada sin respuesta", now)

    def _step_position(self, p: dict, now: int, prices: dict) -> None:
        if p["story"] in self._gaps:   # hueco por reconstruir: nada se gestiona hasta tener sus velas
            return
        px = prices.get(p["coin"])
        gaps = self.fetch_bars is not None
        if px and gaps and now - (_f(p.get("seen")) or now) > GAP_MIN_MS:
            self._open_gap(p, now)
            return
        if p.get("instrument") == "perp" and (px or not gaps):
            # el funding se liquida aunque falte el precio (con el último), salvo si los huecos se reconstruyen: entonces
            # lo liquida la reconstrucción con el precio de cada hora
            self._funding(p, px or p.get("last") or p["entry"], now)
        if px:   # sin precio actualizado no se mueven los stops ni se cierra por tiempo
            p["seen"] = now
            self._manage(p, px, now)

    # ---- huecos: posiciones que pasaron un rato sin gestionarse (hallazgo n.º 47)

    def _open_gap(self, p: dict, now: int) -> None:
        desde = int(_f(p.get("seen")) or p["opened"])
        self._gaps[p["story"]] = {"desde": desde, "intentos": 0, "proximo": 0}
        log.warning("Paper: %s %s sin gestionar desde %s (%s): se reconstruye con velas de 1 min antes de seguir",
                    p["coin"], p["story"], datetime.fromtimestamp(desde / 1000, MAD).strftime("%d-%m %H:%M"),
                    _ago(now - desde))
        self._status("exec", f"{p['coin']}: posición sin gestionar desde las {_hm(desde)} (bot parado o sin precios): "
                             "reconstruyo el hueco con velas de 1 min antes de seguir")

    async def reconcile(self, now: int | None = None) -> int:
        """Reconstruye los huecos pendientes con las velas de 1 min (fetch_bars). Devuelve cuántos se cerraron."""
        if not self._gaps or self.fetch_bars is None:
            return 0
        now = self.clock() if now is None else int(now)
        cerradas = 0
        for story, g in list(self._gaps.items()):
            p = self.positions.get(story)
            if p is None:
                self._gaps.pop(story, None)
                continue
            if now < g["proximo"]:
                continue
            g["intentos"] += 1
            a, b = g["desde"] // 60_000 * 60_000, now // 60_000 * 60_000
            try:
                bars = await self.fetch_bars(p["coin"], a - 2 * 60_000, b) or []
            except Exception as e:
                self._log_err("hueco:" + story, f"Paper: sin velas del hueco de {story} ({e})")
                bars = []
            completo = gap_complete(bars, a, b)
            if not completo and g["intentos"] < GAP_TRIES:
                g["proximo"] = now + GAP_RETRY_S * 1000
                continue
            self._gaps.pop(story, None)
            cerradas += self._apply_gap(p, bars, a, b, now, completo)
        return cerradas

    def _gap_factor(self, p: dict, bars: list, a: int, now: int) -> float:
        """Cociente OKX EU (USDC) / Binance (USDT) al empezar el hueco: el último precio visto frente a la vela de
        entonces; sin él, el precio de ahora frente al último cierre; sin nada, 1."""
        by_t = {int(r[0]): r for r in bars}
        ref = by_t.get(a)
        ref = float(ref[1]) if ref else (float(by_t[a - 60_000][4]) if a - 60_000 in by_t else None)
        last = _f(p.get("last"))
        if ref and last:
            return last / ref
        cur = self._prices(now).get(p["coin"])
        if cur and bars:
            return cur / float(bars[-1][4])
        return 1.0

    def _apply_gap(self, p: dict, bars: list, a: int, b: int, now: int, completo: bool) -> int:
        rows = [r for r in bars if a <= int(r[0]) < b]
        factor = self._gap_factor(p, bars, a, now)
        res = replay_gap(p["side"], p["sl"], p.get("tp2"), p.get("hold_until"), rows, factor)
        info = {"desde": a, "hasta": b, "velas": res["n"], "factor": round(factor, 6), "completo": completo,
                "fuente": "Binance 1 min"}
        d, end = self._dir(p["side"]), res["at"] if res["salida"] else b
        if res["best"] is not None and d * (res["best"] - p["high_water"]) > 0:
            p["high_water"] = res["best"]
        if p.get("instrument") == "perp":   # las liquidaciones del funding de por medio, con el precio de su hora
            close_at = {int(r[0]) + 60_000: float(r[4]) * factor for r in rows}
            nxt = p.get("funding_next") or next_funding_ms(p["opened"])
            while nxt <= end and nxt <= now:
                self._pay_funding(p, nxt, close_at.get(nxt) or p.get("last") or p["entry"], now)
                nxt = p["funding_next"]
        if res["salida"]:
            at = int(res["at"])
            p["hueco"] = info
            if res["salida"] == "objetivo":
                why, px = f"Objetivo ({_n(p['tp2R'], 1)} R)", res["px"]
            elif res["salida"] == "stop":
                why, px = STOP_NAMES[p["sl_kind"]], self._exit_px(p, res["px"])
            else:
                why, px = f"Cierre por tiempo ({p['hold_label']})", self._exit_px(p, res["px"])
            why += f" con el bot sin gestionarla (a las {_hm(at)} Madrid, reconstruido con velas de 1 min de Binance)"
            self._close(p, px, why, now, at=at)
            return 1
        p["huecos"] = [*(p.get("huecos") or []), info][-20:]
        if res["last"] is not None:
            p["last"] = res["last"]
        p["seen"] = now
        p["exec"] = {**p["exec"], "high_water": p["high_water"], "hueco": info}
        txt = (f"Sin gestionar de {_hm(a)} a {_hm(b)} (Madrid): {res['n']} velas de 1 min de Binance sin tocar el stop "
               "ni el objetivo; sigue abierta")
        if not completo:
            txt = (f"Sin gestionar de {_hm(a)} a {_hm(b)} (Madrid) y sin velas completas de ese hueco tras {GAP_TRIES} "
                   f"intentos ({res['n']} velas): sigue abierta en vivo, pero el hueco queda sin reconstruir")
        self._event("manage", "exec", p["story"], p["coin"], f"{p['coin']}: hueco revisado", txt,
                    {"exec": dict(p["exec"])}, now)
        self._save_trade(p)
        log.info("Paper: hueco de %s revisado (%d velas, completo=%s): sigue abierta", p["story"], res["n"], completo)
        return 0

    def _note_stale(self, prices: dict) -> None:
        """Avisa en la web cuando una posición abierta se queda sin precio actualizado (y cuando vuelve)."""
        miss = sorted({p["coin"] for p in self.positions.values()} - set(prices))
        if miss == self._stale_seen:
            return
        self._stale_seen = miss
        if miss:
            self._status("exec", f"Sin precio actualizado de {', '.join(miss)} en OKX EU: stops y cierres en pausa "
                                 "hasta que vuelva")
        else:
            self._exec_status()

    def _guard(self, story: str, fn, *args) -> None:
        try:
            fn(*args)
        except Exception:
            self._log_err("story:" + story, f"Paper: fallo al gestionar {story}; se salta en esta ronda")

    def _log_err(self, key: str, msg: str) -> None:
        """log.exception con freno: un fallo que se repite cada segundo no llena el registro (llamar desde un except)."""
        t = time.monotonic()
        if t - self._err_at.get(key, float("-inf")) >= ERR_LOG_EVERY_S:
            self._err_at[key] = t
            log.exception(msg)

    async def run(self) -> None:
        self.announce()
        last_pf = 0.0
        while True:
            if self._gaps:   # huecos por reconstruir (velas de la red): antes de gestionar
                try:
                    await self.reconcile()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._log_err("hueco", "Paper trading: fallo al reconstruir un hueco")
            try:
                self.step()
            except Exception:
                self._log_err("step", "Paper trading: fallo en la ronda de gestión")
            if time.monotonic() - last_pf >= PORTFOLIO_EVERY_S:
                last_pf = time.monotonic()
                try:   # aparte: la cartera sale aunque falle la ronda
                    self.publish_portfolio()
                except Exception:
                    self._log_err("portfolio", "Paper trading: fallo al publicar la cartera")
            await asyncio.sleep(STEP_EVERY_S)

    # ---- vistas

    def portfolio(self, now: int | None = None) -> dict:
        """Mensaje «portfolio» (contrato 2.5)."""
        now = self.clock() if now is None else now
        prices = self._prices(now)
        eq = self.equity(prices)
        pnl, pct = self.day_pnl(now, prices)
        positions = []
        for p in self.positions.values():
            px = prices.get(p["coin"]) or p.get("last") or p["entry"]
            u = self._upnl(p, px)
            positions.append({
                "story": p["story"], "coin": p["coin"], "side": p["side"], "qty": round(p["qty"], 8), "entry": round(p["entry"], 8),
                "sl": round(p["sl"], 8), "trail_pct": p["trail_pct"], "high_water": round(p["high_water"], 8),
                "opened": p["opened"], "riskUsd": round(p["riskUsd"], 2), "notional": round(p["notional"], 2),
                "headline": p["headline"], "strategy": p["strategy"], "window": p["window"],
                # Añadidos: precio actual, resultado latente (con la comisión de entrada) y datos del plan.
                "id": p["id"], "market": p["market"], "price": px, "upnl": round(u, 2),
                "R": round(u / p["riskUsd"], 3) if p["riskUsd"] else 0.0, "sl_initial": round(p["sl_initial"], 8),
                "tp2": p["tp2"], "be": p["be"], "hold_until": p["hold_until"],
                "tp1": p.get("tp1") if p.get("tp1_frac") else None, "tp1_frac": p.get("tp1_frac", 0.0),
                "tp1_done": p.get("tp1_done", False), "realized": round(p.get("realized", 0.0), 2),
                "be_r": p.get("be_r", 1.0), "trail_start_r": p.get("trail_start_r"),
                "instrument": p.get("instrument") or "spot", "funding": round(p.get("funding", 0.0), 2),
                "tags": p.get("tags"), "reconstruyendo": p["story"] in self._gaps, "huecos": p.get("huecos")})
        return {"type": "portfolio", "ts": now, "equity": round(eq, 2), "start_equity": self.start_equity,
                "day_pnl": round(pnl, 2), "day_pnl_pct": round(pct, 3), "positions": positions,
                "pending": [{"story": i["story"], "deadline": i["deadline"], "mode": i["mode"],
                             "coin": i["ticket"]["coin"], "side": i["ticket"]["side"]} for i in self.pending.values()],
                "scheduled": [{"story": i["story"], "at": i["at"], "expires": i["expires"],
                               "coin": i["ticket"]["coin"], "side": i["ticket"]["side"]} for i in self.scheduled.values()],
                "closed": list(reversed(self.closed[-CLOSED_IN_PORTFOLIO:])),
                "limits": {"risk_per_trade_pct": config.RISK_PER_TRADE_PCT, "max_daily_loss_pct": config.MAX_DAILY_LOSS_PCT,
                           "max_positions": config.MAX_POSITIONS, "day_loss_pct": round(max(0.0, -pct), 3),
                           "halted": bool(self.halt_until), "halt_until": self.halt_until or None},
                "mode": self.mode,
                # Parada manual (halt / resume desde la web); limits.halted es la pausa por pérdida diaria.
                "halted": bool(self.halted), "caps": list(CAPS), "cuenta": self.cuenta}

    def publish_portfolio(self, now: int | None = None, force: bool = False) -> bool:
        """Publica «portfolio» solo si ha cambiado algo además de la hora."""
        pf = self.portfolio(now)
        key = json.dumps({k: v for k, v in pf.items() if k != "ts"}, sort_keys=True, ensure_ascii=False)
        if key == self._pf_key and not force:
            return False
        self._pf_key = key
        self.bus.publish(pf)
        return True

    def results(self) -> dict:
        """Operaciones cerradas y agregados para GET /api/resultados (contrato 3.2)."""
        start = self.start_equity
        trades = sorted(self.closed, key=lambda t: t["closed"])

        def agg(key: str) -> list[dict]:
            groups: dict[str, dict] = {}
            for t in trades:
                g = groups.setdefault(t.get(key) or "–", {"n": 0, "w": 0, "pnl": 0.0, "R": 0.0})
                g["n"] += 1
                g["w"] += t["pnl"] > 0
                g["pnl"] += t["pnl"]
                g["R"] += t.get("R") or 0.0
            rows = [{key: k, "n": g["n"], "win_rate": round(g["w"] / g["n"], 4), "pnl": round(g["pnl"], 2),
                     "avg_R": round(g["R"] / g["n"], 3)} for k, g in groups.items()]
            return sorted(rows, key=lambda r: -r["pnl"])

        eq = start
        curve = [[trades[0]["opened"] if trades else self.clock(), round(start, 2)]]
        for t in trades:
            eq += t["pnl"]
            curve.append([t["closed"], round(eq, 2)])
        days: dict[str, dict] = {}
        for t in trades:
            d = _day(t["closed"]).isoformat()
            x = days.setdefault(d, {"pnl": 0.0, "n": 0})
            x["pnl"] += t["pnl"]
            x["n"] += 1
        daily, base = [], start
        for d in sorted(days):   # % sobre el capital con que empezó cada día (fecha de Madrid)
            x = days[d]
            daily.append({"date": d, "pnl": round(x["pnl"], 2), "pnl_pct": round(x["pnl"] / base * 100, 3) if base else 0.0,
                          "n": x["n"]})
            base += x["pnl"]
        return {"start_equity": start, "cuenta": self.cuenta, "equity": round(self.equity(), 2), "trades": list(reversed(trades)),
                "by_strategy": agg("strategy"), "by_window": agg("window"), "by_coin": agg("coin"),
                "equity_curve": curve, "daily": daily}
