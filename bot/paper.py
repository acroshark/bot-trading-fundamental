"""Paper trading: broker ficticio con precios reales de OKX EU. Recibe los tickets del estratega (patch.strat), pasa
los controles de riesgo y la aprobación, ejecuta, gestiona los stops y publica la cartera (contratos 2.5, 2.6 y 3.2).
Las operaciones se guardan en la tabla trades y las abiertas se recuperan al reiniciar.
Cada ticket puede traer sus salidas (beR, trailStartR, tp1R + tp1Frac, tp2R y el cerrojo lockR + lockFloorR +
lockTrailR; ver exit_rules); sin ellas, como siempre. Con el cerrojo, el ticket puede traer también winEndActive y
holdActiveLabel: el cierre por tiempo pasa a esa hora cuando el cerrojo se activa (p. ej. 24 h sin activar y 7 días
activado, «6 diarias v2»). Una posición con la parcial hecha sigue siendo LA posición de esa moneda («a medias»).
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
Comisiones (29-10-2026, bot/comisiones.py): las del perfil en uso (por defecto OKX EU con cuenta X-Perps). La entrada,
el stop y los cierres a mercado pagan comisión de tomador; el objetivo y la parcial (órdenes límite), de creador. Cada
posición guarda las tasas con que se abrió («comision») y las usa hasta cerrarse; cada operación lleva sus costes
(«costes»: entrada, salidas, funding y total, en USDC y en R) y la cartera, el perfil en uso («comisiones»). Sin cuenta
X-Perps (perfil okx_eu_sin_xperps, o la cuenta real sin X-Perps) no hay cortos ni perpetuos: se vetan («sin_cortos»).
Mandos de la web (server.on_ws_message): decide, close, set_mode, halt / resume (parada manual, persistente), cancel y
set_comisiones (perfil de comisiones, persistente)."""
import asyncio
import hashlib
import json
import logging
import math
import random
import time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from . import comisiones, config, regions
from .bus import Bus
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
COST_SLIP_BPS = 2.0            # deslizamiento supuesto por lado en el coste de ida y vuelta (como el backtest); es
                               # también el máximo del simulado, que se suma al medio diferencial
BE_MARGIN_BPS = 2.0            # margen del break-even sobre el coste de ida y vuelta
STOP_NAMES = {"inicial": "Stop de pérdida", "be": "Stop en break-even", "dinamico": "Stop dinámico",
              "cerrojo": "Stop del cerrojo", "seguimiento": "Stop de seguimiento"}
MODE_TEXT = {"auto": "Aprobación automática (solo paper)", "veto": "Ventana de veto de {s} s",
             "manual": "Apruebas cada orden (caduca en 10 min)"}
INSTRUMENTS = ("spot", "perp")
PERP_MAX_LEVERAGE = 2.0        # nominal en perpetuos ≤ 2 × capital (máximo para particulares en España)
FUNDING_EVERY_MS = 8 * 3600_000   # liquidaciones del funding: 00:00, 08:00 y 16:00 UTC
HALT_WHY = "Paper detenido por ti"   # parada manual desde la web (no es la pausa por pérdida diaria)
CAPS = ("halt", "cancel", "comisiones")   # mandos de la web que admite este paper (portfolio.caps)
SIN_CORTOS_WHY = "Sin cuenta X-Perps"     # principio del veto de un corto o un perpetuo sin X-Perps (código sin_cortos)
# Campos sin los que una operación guardada no se puede cargar (los que escribe _fill / _close).
NEED_OPEN = ("id", "story", "coin", "side", "qty", "entry", "sl", "sl_initial", "sl_kind", "sl_pub", "stop_pct",
             "trail_pct", "high_water", "opened", "riskUsd", "notional", "fee_open", "tp2", "tp2R", "be", "hold_until",
             "hold_label", "headline", "strategy", "window", "market", "exec")
NEED_CLOSED = ("id", "story", "opened", "closed", "pnl")


# ---------------------------------------------------------------- utilidades

def _f(x, default=None):
    """Número finito o default: «inf» y NaN (que json.loads acepta) no valen como stop, objetivo ni cerrojo."""
    try:
        v = float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return default
    return v if v is not None and math.isfinite(v) else default


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


def motivo_codigo(why: str | None, kind: str = "riskko") -> str:
    """Código del catálogo cerrado de decisiones_apertura (docs/04, 2.17) para el motivo con que el paper veta
    (riskko: el primer control que falla de _risk_checks), no ejecuta (blocked: _blockers, maxAgeMs, la parada manual,
    una cancelación o una programada que caduca) o no aprueba (humanko) una orden. Los textos son los de este módulo."""
    t = " ".join(str(why or "").split())
    if t.startswith(HALT_WHY):
        return "otro"
    if t.startswith(SIN_CORTOS_WHY):
        return "sin_cortos"   # el perfil de comisiones no tiene X-Perps: ni cortos ni perpetuos
    if kind == "humanko" or t.startswith("Cancelada"):
        return "aprobacion"   # rechazada o cancelada por ti, o caducada sin respuesta
    if t.startswith("Aprobada ") and "después de enviarla" in t:
        return "aprobacion"   # aprobada tarde (maxAgeMs): el plan ya es viejo
    if t.startswith("Regla del usuario"):
        return "reglas"
    if t.startswith(("Pérdida del día", "Operativa detenida")):
        return "perdida_dia"
    if t.startswith("Posiciones abiertas o en curso") or (t.startswith("Ya hay ") and "posiciones abiertas" in t):
        return "max_posiciones"
    if t.startswith(("Exposición total", "La exposición subiría", "Nominal en perpetuos", "El nominal en perpetuos")):
        return "exposicion"
    if t.startswith("Ya hay una operación en") and "para esta noticia" not in t:
        return "moneda_ocupada"
    return "otro"


def market_of(window: str) -> str | None:
    """Bolsa a la que se refiere una ventana ("Apertura Londres" → "londres")."""
    w = (window or "").lower()
    for m in regions.MARKETS.values():
        if m.key in w.replace(" ", "") or m.name.lower() in w:
            return m.key
    return None


# ---------------------------------------------------------------- salidas (funciones puras, también para el backtest)

def cost_rt_pct(inst: str = "spot") -> float:
    """Coste de ida y vuelta previsto en % del nominal con el perfil de comisiones en uso (comisiones.coste_rt_pct):
    tomador a la entrada y a la salida y 2 pb de deslizamiento por lado (0,24 % al contado con cuenta X-Perps, 0,14 % en
    X-Perps, 0,74 % al contado sin X-Perps)."""
    return comisiones.coste_rt_pct(inst)


def stop_loss_frac(side: str, stop_pct: float, exit_slip_bps: float, fee_pct: float | None = None) -> float:
    """Pérdida al saltar el stop inicial, por unidad de nominal: el recorrido hasta el stop, el deslizamiento al salir y
    las comisiones de entrada y salida (las dos a mercado: fee_pct, por defecto la de tomador del contado del perfil en
    uso). Un hueco que salte más allá del stop no se puede prever."""
    fee = comisiones.tasa("spot", "tomador") if fee_pct is None else fee_pct
    d = 1 if side == "long" else -1
    k = (1 - d * stop_pct / 100) * (1 - d * exit_slip_bps / 10000)   # precio de salida / precio de entrada
    return abs(1 - k) + fee / 100 * (1 + k)


def be_offset_pct(inst: str = "spot") -> float:
    """Distancia del break-even a la entrada: coste de ida y vuelta + 2 pb (no la entrada pelada)."""
    return cost_rt_pct(inst) + BE_MARGIN_BPS / 100


def rule_win_ok(r: float | None, stop_pct: float | None, inst: str = "spot") -> bool:
    """Regla del usuario para una salida que gana (objetivo, parcial o suelo del cerrojo) a r R con stop de stop_pct %:
    r ≥ RULE_MIN_RR (2R) y r × stop − coste de ida y vuelta (cost_rt_pct del instrumento) ≥ RULE_MIN_NET_PCT (3 %)."""
    r, sp = _f(r), _f(stop_pct)
    return bool(r and sp and r >= config.RULE_MIN_RR - 1e-9
                and r * sp - cost_rt_pct(inst) >= config.RULE_MIN_NET_PCT - 1e-9)


def exit_rules(t: dict) -> dict:
    """Salidas de un ticket. Un ticket sin estos campos se gestiona como siempre (break-even a +1 R en la entrada y stop
    dinámico desde el principio).
    - beR: R desde el que el stop pasa a break-even; null o ≤ 0 = nunca. Con el campo, el nivel cubre costes.
    - trailStartR: R desde el que actúa el stop dinámico (null = desde la entrada).
    - tp1R + tp1Frac: salida parcial de esa fracción en tp1 (0 = sin parcial). tp2R: objetivo final (null = sin tope).
    - lockR + lockFloorR (+ lockTrailR): cerrojo. Al llegar a +lockR R el stop sube al suelo (+lockFloorR R) y, con
      lockTrailR, desde ahí sigue al mejor precio a esa distancia en R, solo por encima del suelo (nunca baja). Un lockR
      por debajo del suelo sube al suelo; sin suelo no hay cerrojo.
    Con las reglas del usuario («o gano más de 2 o pierdo 1»): nada mueve el stop antes del cerrojo (sin break-even ni
    stop dinámico); la parcial solo si su objetivo cumple rule_win_ok (si no, no hay parcial) y el cerrojo solo si su
    suelo la cumple (si no, no hay cerrojo; entonces el ticket necesita un objetivo fijo ≥ 2R: _invalid). Lo que el
    ticket pedía y la regla descarta va en «dropped» (textos), para decirlo al abrir. Un valor no finito no vale."""
    trail = _f(t.get("trailPct"))
    inst = instrument_of(t)
    if "beR" in t:
        be_r = _f(t.get("beR"))
        be_r, be_pct = (be_r if be_r and be_r > 0 else None), be_offset_pct(inst)
    else:
        be_r, be_pct = 1.0, 0.0
    ts, tp1R, tp2R = _f(t.get("trailStartR")), _f(t.get("tp1R")), _f(t.get("tp2R"))
    tp1R = tp1R if tp1R and tp1R > 0 else None
    frac = min(1.0, max(0.0, _f(t.get("tp1Frac"), 0.0))) if tp1R else 0.0
    lock, floor, ltrail = _f(t.get("lockR")), _f(t.get("lockFloorR")), _f(t.get("lockTrailR"))
    if lock and lock > 0 and floor and floor > 0:
        lock = max(lock, floor)
    else:
        lock = floor = None
    ltrail = ltrail if lock and ltrail and ltrail > 0 else None
    dropped = []
    if config.USER_RULES:   # o gano más de 2 o pierdo 1: nada mueve el stop antes de ganar el mínimo
        trail, be_r, ts = None, None, None
        stop = _f(t.get("stopPct"))
        if frac and not rule_win_ok(tp1R, stop, inst):
            dropped.append(f"salida parcial en {_n(tp1R, 1)} R descartada por tu regla (≥ {_n(config.RULE_MIN_RR, 0)}R "
                           f"y ≥ {_n(config.RULE_MIN_NET_PCT, 0)} % neto)")
            frac = 0.0
        if lock and not rule_win_ok(floor, stop, inst):
            dropped.append(f"cerrojo con suelo en {_n(floor, 1)} R descartado por tu regla (≥ {_n(config.RULE_MIN_RR, 0)}R "
                           f"y ≥ {_n(config.RULE_MIN_NET_PCT, 0)} % neto)")
            lock = floor = ltrail = None
    return {"trail": trail if trail and trail > 0 else None, "be_r": be_r, "be_pct": be_pct,
            "trail_start_r": ts if ts and ts > 0 else None, "tp1R": tp1R, "tp1_frac": frac,
            "tp2R": tp2R if tp2R and tp2R > 0 else None, "lock_r": lock, "floor_r": floor, "lock_trail_r": ltrail,
            "dropped": dropped}


def lock_update(d: int, entry: float, stop_pct: float, best: float, stop: float, locked: bool, lock_r: float | None,
                floor_r: float | None, trail_r: float | None = None) -> tuple[float, bool, str | None]:
    """Cerrojo tras un nuevo extremo favorable `best` (el stop nunca baja). Devuelve (stop, activado, tipo del
    movimiento o None).
    - Se activa cuando best llega a +lock_r R: el stop sube al suelo, entrada ± floor_r R («cerrojo»). Antes, nada.
    - Activado y con trail_r: el stop sigue a best a trail_r R por detrás, solo por encima del suelo («seguimiento»)."""
    if not lock_r or not floor_r:
        return stop, locked, None
    kind = None
    r = entry * stop_pct / 100
    if not locked and d * (best - (entry + d * lock_r * r)) >= 0:
        locked = True
        lvl = entry + d * floor_r * r
        if d * (lvl - stop) > 0:
            stop, kind = lvl, "cerrojo"
    if locked and trail_r:
        lvl = best - d * trail_r * r
        if d * (lvl - stop) > 0:
            stop, kind = lvl, "seguimiento"
    return stop, locked, kind


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


# ---------------------------------------------------------------- broker

class PaperBroker:
    def __init__(self, bus: Bus, store: Store, get_market, get_orderflow=None, clock=now_ms, slip_bps=None):
        self.bus = bus
        self.store = store
        self.get_market = get_market                          # () -> último "market" (precios y diferencial OKX EU)
        self.get_orderflow = get_orderflow or (lambda: None)  # () -> último "orderflow" (diferencial del libro)
        self.clock = clock                                    # () -> ms; inyectable en las pruebas
        self.slip_bps = slip_bps or (lambda: random.uniform(0.5, 2.0))
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
        com = self._get_state("comisiones")   # perfil de comisiones elegido desde la web (set_comisiones)
        if isinstance(com, dict) and com.get("perfil") in comisiones.PERFILES:
            if com.get("config") == comisiones.config_perfil():
                comisiones.elegir(com["perfil"])   # el último cambio hecho desde la web sigue valiendo
            else:   # BOT_COMISIONES ha cambiado después: manda la configuración y queda guardada
                if com["perfil"] != comisiones.config_perfil():
                    log.warning("Paper: BOT_COMISIONES ha cambiado desde que elegiste «%s» en la web: se usa «%s»",
                                com["perfil"], comisiones.config_perfil())
                comisiones.elegir(comisiones.config_perfil())
                self._set_state("comisiones", {"perfil": comisiones.config_perfil(), "config": comisiones.config_perfil()})
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
        return funding_8h((self._market().get("flow") or {}).get(coin))

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
        fee = comisiones.tasa(instrument_of(t), "tomador")   # entrada y stop, a mercado
        loss = stop_loss_frac(t.get("side"), stop, self._exit_slip_bps(t.get("coin")), fee)
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
                f"{_n(config.MAX_DAILY_LOSS_PCT)} % de pérdida al día, {config.MAX_POSITIONS} posiciones · comisiones: "
                f"{comisiones.perfil()['nombre']} ({comisiones.texto()})")

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

    def set_comisiones(self, perfil: str) -> dict:
        """Perfil de comisiones desde la web ({"type": "comisiones", "perfil"}), guardado para después de un reinicio
        (salvo que cambie BOT_COMISIONES). Vale para las entradas nuevas: cada posición abierta sigue con las tasas con
        que se abrió. Con la comisión real de la cuenta leída (modo real con claves), manda esa."""
        ok, why = comisiones.elegir(perfil) if isinstance(perfil, str) else (False, "Perfil no válido")
        if ok:
            self._set_state("comisiones", {"perfil": perfil, "config": comisiones.config_perfil()})
            log.warning("Paper: perfil de comisiones «%s» (%s)", perfil, comisiones.texto(perfil))
            self._status("risk", self._risk_text())
        self._after_command()
        return {"ok": ok, "why": why, "comisiones": comisiones.vista()}

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
                        {"risk": {"checks": checks, "ok": False, "why": bad["t"],
                                  "motivo_codigo": motivo_codigo(bad["t"])}}, now)
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
        bad = self._sin_cortos(t)
        if bad:
            return bad
        if not 0 < stop <= 20:
            return f"Stop no válido: {_n(stop)} %"
        if risk <= 0:
            return "Riesgo por operación no válido"
        if config.USER_RULES:
            if stop < config.RULE_MIN_STOP_PCT - 1e-9:
                return f"Regla del usuario: stop de {_n(stop)} % por debajo del mínimo de {_n(config.RULE_MIN_STOP_PCT)} %"
            rr = _f(t.get("tp2R"))
            if rr and rr < config.RULE_MIN_RR - 1e-9:
                return f"Regla del usuario: objetivo de {_n(rr)} R por debajo del mínimo de {_n(config.RULE_MIN_RR)} R"
            bad = self._regla_neta(t, stop)
            if bad:
                return bad
        if t.get("timing") not in ("now", "window"):
            return f"Momento de entrada desconocido: {t.get('timing')}"
        if t.get("timing") == "window" and not _f(t.get("winAt")):
            return "Entrada programada sin hora (winAt)"
        if story in self.positions or story in self.pending or story in self.scheduled:
            return "Ya hay una operación en curso para esta noticia"
        return None

    # Lo que depende del perfil de comisiones en uso lo mira submit (_invalid) y otra vez _blockers al ejecutar: el
    # perfil puede cambiar entre medias (desde la web, o solo, cuando el conector lee la comisión real de la cuenta), y
    # una pendiente (veto o a mano) o una programada no entra con un perfil con el que no se habría aceptado.

    @staticmethod
    def _sin_cortos(t: dict) -> str | None:
        """Sin cuenta X-Perps (perfil sin X-Perps, o la cuenta real sin ellos) no hay cortos ni perpetuos."""
        if (t.get("side") == "short" or instrument_of(t) == "perp") and not comisiones.hay_cortos():
            return (f"{SIN_CORTOS_WHY}: no hay cortos ni perpetuos con el perfil de comisiones "
                    f"«{comisiones.perfil()['nombre']}» (el contado cuesta {_n(comisiones.tasa('spot', 'tomador'))} % "
                    "como tomador)")
        return None

    @staticmethod
    def _regla_neta(t: dict, stop: float) -> str | None:
        """Regla del usuario que depende del coste (el perfil de comisiones): el objetivo fijo deja ≥ 3 % neto o, sin
        él, hay un cerrojo cuyo suelo lo cumple (exit_rules lo quita si no)."""
        inst = instrument_of(t)
        rr = _f(t.get("tp2R"))
        if rr and not rule_win_ok(rr, stop, inst):   # el objetivo deja ≥ 3 % neto de costes
            return (f"Regla del usuario: el objetivo de {_n(rr)} R con stop de {_n(stop)} % deja "
                    f"{_n(rr * stop - cost_rt_pct(inst))} % neto de costes (mínimo {_n(config.RULE_MIN_NET_PCT, 0)} %)")
        if not rr and exit_rules(t)["lock_r"] is None:   # sin objetivo fijo, solo con un cerrojo que cumpla
            if _f(t.get("lockR")):
                return (f"Regla del usuario: sin objetivo fijo y el cerrojo no vale (su suelo tiene que ser ≥ "
                        f"{_n(config.RULE_MIN_RR)} R y dejar ≥ {_n(config.RULE_MIN_NET_PCT, 0)} % neto de costes)")
            return f"Regla del usuario: sin objetivo fijo ni cerrojo (hace falta al menos {_n(config.RULE_MIN_RR, 0)}R)"
        return None

    def _invalid_perfil(self, t: dict) -> str | None:
        """Al ejecutar: lo que depende del perfil de comisiones en uso (cortos y regla del 3 % neto), con el aviso de
        que el perfil ha cambiado desde que se envió (al enviarla, pasó)."""
        bad = self._sin_cortos(t) or (self._regla_neta(t, _f(t.get("stopPct"), 0.0)) if config.USER_RULES else None)
        if bad:
            return f"{bad} · el perfil de comisiones ha cambiado desde que se envió"
        return None

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
        """Lo que impide entrar ahora: (definitivo, pasajero). Lo pasajero solo hace esperar a una entrada programada.
        Lo que depende del perfil de comisiones (cortos, regla del 3 % neto) se vuelve a mirar: es definitivo."""
        t = item["ticket"]
        coin = t["coin"]
        self._check_halt(now, prices)
        eq = self.equity(prices)
        hard = soft = None
        perfil = self._invalid_perfil(t)
        if perfil:
            hard = perfil
        elif self.halt_until:
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
                    {"exec": {**(item.get("exec") or {}), "blocked": True, "why": why,
                              "motivo_codigo": motivo_codigo(why, "blocked")}}, now)
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
        tas = comisiones.tasas(inst)   # las tasas con que se abre: valen hasta cerrarla aunque cambie el perfil
        com = {"perfil": tas["perfil"], "creador": tas["creador"], "tomador": tas["tomador"]}
        stop_loss = notional * stop_loss_frac(side, stop, self._exit_slip_bps(coin), com["tomador"])   # en el stop
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
               "trail_pct": trail, "high_water": px, "last": px, "opened": now, "riskUsd": notional * stop / 100,
               "notional": notional, "fee_open": notional * com["tomador"] / 100, "comision": com,
               "tp1": px + d * r * tp1R if tp1R else None, "tp2": px + d * r * tp2R if tp2R else None, "tp2R": tp2R,
               "be": False, "hold_until": hold_until, "hold_label": hold_label, "headline": item["headline"],
               "strategy": item["strategy"], "window": item["window"], "market": item["market"],
               "news_score": item.get("news_score"), "equity_open": eq,
               # Salidas del ticket (exit_rules) y salidas parciales ya hechas.
               "qty0": qty, "tp1R": rules["tp1R"], "tp1_frac": rules["tp1_frac"], "tp1_done": False,
               "be_r": rules["be_r"], "be_pct": rules["be_pct"], "trail_start_r": rules["trail_start_r"],
               "realized": 0.0, "fees_exit": 0.0, "partials": [],
               # Cerrojo (lock_update): activación, suelo y seguimiento en R; con él activado, el cierre por tiempo
               # pasa a hold_active (winEndActive del ticket) si es más tarde.
               "lock_r": rules["lock_r"], "floor_r": rules["floor_r"], "lock_trail_r": rules["lock_trail_r"],
               "locked": False, "locked_at": None, "floor": px + d * r * rules["floor_r"] if rules["lock_r"] else None,
               "hold_active": None, "hold_active_label": None}
        end_act = _f(t.get("winEndActive"))
        if rules["lock_r"] and end_act and end_act > hold_until:
            pos["hold_active"] = int(end_act)
            pos["hold_active_label"] = str(t.get("holdActiveLabel") or "cerrojo activado")
        if isinstance(t.get("tags"), dict):   # etiquetas del motor (noticia, apertura anterior…) para Resultados
            pos["tags"] = dict(t["tags"])
        pos["instrument"] = inst
        if inst == "perp":   # funding: cobrado (+) o pagado (−) en cada liquidación mientras siga abierta
            pos.update(funding=0.0, funding_n=0, funding_next=next_funding_ms(now),
                       funding_rate=self._funding_rate(coin))
        ex.update(px=px, slip=round(slip, 3), ms=round((time.perf_counter() - t0) * 1000, 1), notional=round(notional, 2),
                  qty=qty, sl=sl, tp1=pos["tp1"], tp2=pos["tp2"], trail=trail, tp1Frac=rules["tp1_frac"],
                  beR=rules["be_r"], trailStartR=rules["trail_start_r"], stopLossUsd=round(stop_loss, 2),
                  instrument=inst, lockR=rules["lock_r"], floorR=rules["floor_r"], lockTrailR=rules["lock_trail_r"],
                  floor=pos["floor"], holdUntil=hold_until, holdActive=pos["hold_active"],
                  comision=dict(com), feeOpen=round(pos["fee_open"], 4),
                  **({"descartado": list(rules["dropped"])} if rules["dropped"] else {}))
        pos["exec"] = ex
        self.positions[story] = pos
        self._save_trade(pos)
        plan = ""
        if pos["tp1_frac"]:
            plan += f" · parcial del {_n(pos['tp1_frac'] * 100, 0)} % en {_px(pos['tp1'])}"
        if pos["tp2"]:
            plan += f" · objetivo {_px(pos['tp2'])}"
        if pos["lock_r"]:
            plan += (f" · cerrojo: a +{_n(pos['lock_r'], 1)} R el stop sube a {_px(pos['floor'])} "
                     f"(+{_n(pos['floor_r'], 1)} R)"
                     + (f" y sigue al precio a {_n(pos['lock_trail_r'], 1)} R" if pos["lock_trail_r"] else ""))
            if pos["hold_active"]:
                plan += f"; activado, como mucho hasta el {datetime.fromtimestamp(pos['hold_active'] / 1000, MAD):%d-%m %H:%M}"
        for txt in rules["dropped"]:   # lo que pedía el ticket y la regla del usuario no deja (se dice, no se calla)
            plan += f" · {txt}"
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
                    f" · comisión de entrada {_n(pos['fee_open'])} {config.QUOTE} ({_n(com['tomador'])} % tomador)"
                    f" · riesgo {_n(pos['riskUsd'])} {config.QUOTE} ({_n(stop_loss)} con costes) · stop {_px(sl)}" + plan,
                    {"exec": dict(ex), "trade_id": pos["id"]}, now)
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
        # Cerrojo (lock_update): antes de +lock_r R no mueve nada; activado, suelo y seguimiento por encima del suelo.
        lock_now, lock_moved, extra = False, False, {}
        if p.get("lock_r"):
            was_locked = bool(p.get("locked"))
            sl, p["locked"], kind = lock_update(d, p["entry"], p["stop_pct"], p["high_water"], p["sl"], was_locked,
                                                p["lock_r"], p.get("floor_r"), p.get("lock_trail_r"))
            if kind:
                p["sl"], p["sl_kind"] = sl, kind
                lock_moved = True
            lock_now = p["locked"] and not was_locked
            if lock_now:
                p["locked_at"] = now
                if p.get("hold_active") and p["hold_active"] > p["hold_until"]:
                    p["hold_until"], p["hold_label"] = p["hold_active"], p.get("hold_active_label") or "cerrojo activado"
                extra["lock"] = {"at": now, "lock_r": p["lock_r"], "floor_r": p.get("floor_r"),
                                 "trail_r": p.get("lock_trail_r"), "floor": p.get("floor"),
                                 "hold_until": p["hold_until"]}
        if be_now or lock_now or abs(p["sl"] - p["sl_pub"]) >= MANAGE_STEP_R * r:
            p["sl_pub"] = p["sl"]
            p["exec"] = {**p["exec"], "sl": p["sl"], "be": p["be"], "high_water": p["high_water"],
                         **({"locked": True, "holdUntil": p["hold_until"]} if p.get("locked") else {})}
            lock = d * (p["sl"] - p["entry"]) / r
            if lock_now:
                title = f"{coin}: cerrojo a +{_n(p['floor_r'], 1)} R"
                tr = p.get("lock_trail_r")
                text = (f"+{_n(p['lock_r'], 1)} R alcanzado: el stop sube a {_px(p['sl'])} (asegura {_sg(lock)} R en lo "
                        "que queda)" + (f" y desde ahí sigue al precio a {_n(tr, 1)} R, sin bajar nunca" if tr else "")
                        + f" · cierre por tiempo: {p['hold_label']} ({_hm(p['hold_until'])} del "
                        f"{datetime.fromtimestamp(p['hold_until'] / 1000, MAD):%d-%m} Madrid)")
            elif p["sl_kind"] == "seguimiento":
                title = f"{coin}: stop de seguimiento a {_px(p['sl'])}"
                text = (f"A {_n(p.get('lock_trail_r') or 0.0, 1)} R del {'máximo' if d > 0 else 'mínimo'} de "
                        f"{_px(p['high_water'])} (por encima del suelo del cerrojo) · asegura {_sg(lock)} R")
            elif be_now:
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
            self._event("manage", "exec", p["story"], coin, title, text, {"exec": dict(p["exec"]), **extra}, now)
            self._save_trade(p)
        elif lock_moved:   # el seguimiento sube el stop menos de 0,25 R: se guarda ya, para que un reinicio no lo baje
            self._save_trade(p)

        if p.get("tp1_frac") and not p.get("tp1_done") and p.get("tp1") and d * (px - p["tp1"]) >= 0:
            if self._partial(p, now):   # parcial del 100 %: ya está cerrada
                return
        why = exit_px = None
        limite = False
        if p["tp2"] and d * (px - p["tp2"]) >= 0:   # orden límite: sale al precio del objetivo y paga como creador
            why, exit_px, limite = f"Objetivo ({_n(p['tp2R'], 1)} R)", p["tp2"], True
        elif d * (px - p["sl"]) <= 0:
            why, exit_px = STOP_NAMES.get(p["sl_kind"], "Stop"), self._exit_px(p, px)
        elif now >= p["hold_until"]:
            why, exit_px = f"Cierre por tiempo ({p['hold_label']})", self._exit_px(p, px)
        if why:
            self._close(p, exit_px, why, now, limite)

    def _funding(self, p: dict, px: float, now: int) -> None:
        """Perpetuo: en cada liquidación pasada desde la última, cobra o paga el funding sobre el nominal a precio de
        ahora, con la tasa de market.flow o, sin ella, la última conocida (sin ninguna, esa liquidación no se cobra)."""
        rate = self._funding_rate(p["coin"])
        if rate is not None:
            p["funding_rate"] = rate
        nxt = p.get("funding_next") or next_funding_ms(p["opened"])
        while now >= nxt:
            at, nxt = nxt, nxt + FUNDING_EVERY_MS
            p["funding_next"] = nxt
            r, notional = p.get("funding_rate"), p["qty"] * px
            if r is None:
                log.warning("Paper: %s sin tasa de funding a las %s: esa liquidación no se cuenta", p["story"], _hm(at))
                continue
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
            self._close(p, px, f"Objetivo ({_n(p.get('tp1R') or 0.0, 1)} R)", now, limite=True)
            return True
        fee = q * px * self._tasas(p)["creador"] / 100   # orden límite: creador
        net = d * (px - p["entry"]) * q - fee
        p["qty"] -= q
        p["realized"] = p.get("realized", 0.0) + net
        p["fees_exit"] = p.get("fees_exit", 0.0) + fee
        part = {"qty": q, "px": px, "at": now, "pnl": round(net, 4), "fee": round(fee, 4), "tipo": "creador",
                "why": f"Objetivo parcial ({_n(p.get('tp1R') or 0.0, 1)} R)"}
        part["R"] = round(net / p["riskUsd"], 4) if p.get("riskUsd") else None   # en R de la posición entera
        part["tp1R"] = p.get("tp1R")
        p["partials"] = [*p.get("partials", []), part]
        p["tp1_done"] = True
        p["exec"] = {**p["exec"], "qty": p["qty"], "tp1_done": True, "partial": part}
        rest = ""
        if p.get("locked"):
            sure = d * (p["sl"] - p["entry"]) / (p["entry"] * p["stop_pct"] / 100)
            rest = f" con el cerrojo: stop en {_px(p['sl'])} (asegura {_sg(sure)} R)"
        elif p.get("lock_r"):
            rest = f" (el cerrojo se activa a +{_n(p['lock_r'], 1)} R)"
        # «partial» (solo en este evento) para el registro de decisiones: la parcial y si el resto ya va con el cerrojo
        self._event("manage", "exec", p["story"], coin, f"{coin}: salida parcial en {_px(px)}",
                    f"{'Vendidos' if d > 0 else 'Comprados'} {_qty(coin, q)} {coin} ({_n(p['tp1_frac'] * 100, 0)} % de la "
                    f"posición) a {_px(px)}: {_sg(net)} {config.QUOTE} con comisión · quedan {_qty(coin, p['qty'])} {coin}"
                    + rest,
                    {"exec": dict(p["exec"]),
                     "partial": {**part, "frac": p["tp1_frac"], "qty_left": p["qty"], "locked": bool(p.get("locked")),
                                 "sl": p["sl"], "floor": p.get("floor"), "hold_until": p["hold_until"]}}, now)
        self._save_trade(p)
        return False

    @staticmethod
    def _tasas(p: dict) -> dict:
        """Tasas con que se abrió la posición (en % por lado). Una abierta antes de las comisiones por perfil pagó
        config.FEE_PCT en los dos lados y sigue así."""
        c = p.get("comision")
        if isinstance(c, dict) and _f(c.get("tomador")) is not None:
            return {"perfil": c.get("perfil"), "tomador": _f(c["tomador"]),
                    "creador": _f(c.get("creador"), _f(c["tomador"]))}
        return {"perfil": None, "tomador": config.FEE_PCT, "creador": config.FEE_PCT}

    @staticmethod
    def _costes(entrada: float, salidas: float, funding: float, risk: float | None, perfil: str | None) -> dict:
        """Costes de una operación en USDC y en R: comisión de entrada y de salidas, funding (+ cobrado, − pagado) y
        total = comisiones − funding (lo que costó)."""
        comm = entrada + salidas
        total = comm - funding
        out = {"perfil": perfil, "entrada": round(entrada, 4), "salidas": round(salidas, 4), "comisiones": round(comm, 4),
               "funding": round(funding, 4), "total": round(total, 4)}
        for k, v in (("entrada", entrada), ("salidas", salidas), ("funding", funding), ("total", total)):
            out[f"{k}_R"] = round(v / risk, 4) if risk else None
        return out

    def _costes_pos(self, p: dict, px: float) -> dict:
        """Costes de una posición abierta: los ya pagados (entrada, parciales y funding), lo que costaría salir ahora a
        mercado con lo que queda (tomador, sin el deslizamiento) y lo que costaría salir en el objetivo (creador)."""
        tas = self._tasas(p)
        out = self._costes(p["fee_open"], p.get("fees_exit", 0.0), p.get("funding", 0.0), p.get("riskUsd"),
                           tas["perfil"])
        salir = p["qty"] * px * tas["tomador"] / 100
        out.update(tomador=tas["tomador"], creador=tas["creador"], salir_ahora=round(salir, 4),
                   salir_ahora_R=round(salir / p["riskUsd"], 4) if p.get("riskUsd") else None,
                   objetivo=round(p["qty"] * p["tp2"] * tas["creador"] / 100, 4) if p.get("tp2") else None)
        return out

    def _costes_trade(self, t: dict) -> dict:
        """Costes de una operación cerrada; las cerradas antes de las comisiones por perfil no los traen y se sacan de
        lo que guardaron (pagaron config.FEE_PCT a la entrada): van con «estimado»."""
        if isinstance(t.get("costes"), dict):
            return t["costes"]
        fund = _f(t.get("funding"), 0.0) or 0.0
        comm = _f(t.get("commissions"))
        comm = (_f(t.get("fees"), 0.0) or 0.0) + fund if comm is None else comm
        entrada = min(comm, (_f(t.get("notional"), 0.0) or 0.0) * config.FEE_PCT / 100)
        out = self._costes(entrada, comm - entrada, fund, _f(t.get("riskUsd")), None)
        out["estimado"] = True
        return out

    def _con_costes(self, t: dict) -> dict:
        """La operación cerrada con «costes» y «bruto» (sin tocar la guardada)."""
        if isinstance(t.get("costes"), dict) and "bruto" in t:
            return t
        c = self._costes_trade(t)
        return {**t, "costes": c, "bruto": round((_f(t.get("pnl"), 0.0) or 0.0) + c["total"], 4)}

    def _close(self, p: dict, exit_px: float, why: str, now: int, limite: bool = False) -> dict:
        """Cierra lo que queda (limite: con una orden límite, el objetivo, que paga como creador; si no, a mercado, como
        tomador). pnl, comisiones y R incluyen las salidas parciales; qty es la inicial y exit, el precio medio de todas
        las salidas (así pnl = ±(exit − entry) × qty − comisiones también con parciales)."""
        d, qty = self._dir(p["side"]), p["qty"]
        parts = p.get("partials") or []
        tas = self._tasas(p)
        fee_last = qty * exit_px * tas["creador" if limite else "tomador"] / 100
        comm = p["fee_open"] + p.get("fees_exit", 0.0) + fee_last
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
                 "opened": p["opened"], "closed": now, "reason": why, "pnl": round(pnl, 4),
                 "pnl_pct": round(pnl / p["notional"] * 100, 4), "R": round(R, 4), "fees": round(fees, 4),
                 "headline": p["headline"], "news_score": p.get("news_score"),
                 # Añadidos: riesgo y nominal, resultado en % del capital, stop final y extremo favorable.
                 "riskUsd": round(p["riskUsd"], 4), "notional": round(p["notional"], 2),
                 "equity_pct": round(pnl / (p.get("equity_open") or self.start_equity) * 100, 4),
                 "sl_final": round(p["sl"], 8), "high_water": round(p["high_water"], 8)}
        if parts:   # detalle de las salidas: las parciales y la última (el resto de la posición)
            trade["partials"] = parts
            trade["exit_last"] = round(exit_px, 8)
        if p.get("lock_r"):   # cerrojo: si llegó a activarse y con qué suelo y seguimiento
            trade.update(lock_r=p["lock_r"], floor_r=p.get("floor_r"), lock_trail_r=p.get("lock_trail_r"),
                         locked=bool(p.get("locked")), locked_at=p.get("locked_at"))
        if p.get("tags"):
            trade["tags"] = p["tags"]
        trade.update(instrument=p.get("instrument") or "spot", funding=round(fund, 4), commissions=round(comm, 4))
        trade["costes"] = self._costes(p["fee_open"], p.get("fees_exit", 0.0) + fee_last, fund, p["riskUsd"],
                                       tas.get("perfil"))
        trade["costes"]["salida_tipo"] = "creador" if limite else "tomador"
        trade["bruto"] = round(pnl + fees, 4)   # antes de comisiones y funding (con el deslizamiento en los precios)
        del self.positions[p["story"]]
        self.closed.append(trade)
        self._save_trade(trade, now)
        result = {"coin": trade["coin"], "side": trade["side"], "qty": trade["qty"], "entry": trade["entry"],
                  "exit": trade["exit"], "why": why, "pnl": trade["pnl"], "pnl_pct": trade["pnl_pct"], "R": trade["R"],
                  "fees": trade["fees"], "opened": trade["opened"], "closedAt": now, "riskUsd": trade["riskUsd"],
                  "notional": trade["notional"], "headline": trade["headline"], "strategy": trade["strategy"],
                  "window": trade["window"]}
        result["costes"] = dict(trade["costes"])
        result["bruto"] = trade["bruto"]
        fund_t = (f" (con funding {_sg(fund)} {config.QUOTE} de {p.get('funding_n', 0)} liquidaciones)"
                  if p.get("instrument") == "perp" else "")
        self._event("closed", "exec", p["story"], p["coin"], f"{p['coin']} cerrada: {_sg(pnl)} {config.QUOTE}",
                    f"{why} · {_sg(R)} R · comisiones {_n(fees)} {config.QUOTE}{fund_t} · bruto {_sg(pnl + fees)} "
                    f"{config.QUOTE}", {"result": result, "trade_id": trade["id"]}, now,
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
            self.save_positions()

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
        px = prices.get(p["coin"])
        if p.get("instrument") == "perp":   # el funding se liquida aunque falte el precio (con el último)
            self._funding(p, px or p.get("last") or p["entry"], now)
        if px:   # sin precio actualizado no se mueven los stops ni se cierra por tiempo
            self._manage(p, px, now)

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

    def save_positions(self) -> None:
        """Guarda ya todas las posiciones abiertas (stop, extremo, cerrojo…), cada una aparte."""
        for story, p in list(self.positions.items()):
            self._guard(story, self._save_trade, p)

    async def run(self) -> None:
        self.announce()
        last_pf = 0.0
        try:
            while True:
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
        finally:   # al parar el bot: lo último de cada posición (lo que va entre los guardados de cada 30 s) no se pierde
            self.save_positions()

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
                "tags": p.get("tags"),
                # Cerrojo: activación, suelo y seguimiento (R), si ya está activado, el precio del suelo, el cierre por
                # tiempo de ahora (hold_until) y el que tendrá activado (hold_active); qty0 = cantidad inicial.
                "hold_label": p.get("hold_label"), "qty0": round(p.get("qty0") or p["qty"], 8),
                "lock_r": p.get("lock_r"), "floor_r": p.get("floor_r"), "lock_trail_r": p.get("lock_trail_r"),
                "locked": bool(p.get("locked")), "floor": p.get("floor"), "hold_active": p.get("hold_active"),
                "sl_kind": p.get("sl_kind"),
                # Costes (comisiones por perfil): entrada, salidas, funding, total y lo que costaría salir ahora.
                "costes": self._costes_pos(p, px)})
        return {"type": "portfolio", "ts": now, "equity": round(eq, 2), "start_equity": self.start_equity,
                "day_pnl": round(pnl, 2), "day_pnl_pct": round(pct, 3), "positions": positions,
                "pending": [{"story": i["story"], "deadline": i["deadline"], "mode": i["mode"],
                             "coin": i["ticket"]["coin"], "side": i["ticket"]["side"]} for i in self.pending.values()],
                "scheduled": [{"story": i["story"], "at": i["at"], "expires": i["expires"],
                               "coin": i["ticket"]["coin"], "side": i["ticket"]["side"]} for i in self.scheduled.values()],
                "closed": [self._con_costes(t) for t in reversed(self.closed[-CLOSED_IN_PORTFOLIO:])],
                "limits": {"risk_per_trade_pct": config.RISK_PER_TRADE_PCT, "max_daily_loss_pct": config.MAX_DAILY_LOSS_PCT,
                           "max_positions": config.MAX_POSITIONS, "day_loss_pct": round(max(0.0, -pct), 3),
                           "halted": bool(self.halt_until), "halt_until": self.halt_until or None},
                "mode": self.mode,
                # Parada manual (halt / resume desde la web); limits.halted es la pausa por pérdida diaria.
                "halted": bool(self.halted), "caps": list(CAPS), "cuenta": self.cuenta,
                # Perfil de comisiones en uso (bot/comisiones.py): tasas, si hay cortos y el aviso sin X-Perps.
                "comisiones": comisiones.vista()}

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
        trades = sorted((self._con_costes(t) for t in self.closed), key=lambda t: t["closed"])

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
        comm = sum(t["costes"]["comisiones"] for t in trades)
        fund = sum(t["costes"]["funding"] for t in trades)
        neto = sum(t["pnl"] for t in trades)
        return {"start_equity": start, "cuenta": self.cuenta, "equity": round(self.equity(), 2), "trades": list(reversed(trades)),
                "by_strategy": agg("strategy"), "by_window": agg("window"), "by_coin": agg("coin"),
                "equity_curve": curve, "daily": daily,
                # Comisiones en el sistema: lo pagado en comisiones, el funding (+ cobrado, − pagado), el resultado
                # bruto (antes de comisiones y funding) y el neto (el de verdad), de las cerradas; y el perfil en uso.
                "comisiones_total": round(comm, 2), "funding_total": round(fund, 2),
                "costes_total": round(comm - fund, 2), "bruto": round(neto + comm - fund, 2), "neto": round(neto, 2),
                "costes_estimados": sum(1 for t in trades if t["costes"].get("estimado")),
                "comisiones": comisiones.vista()}
