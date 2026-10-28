"""Entrada por noticia fiable confirmada por el precio (contrato 2.16). SOLO PAPER: nunca opera con dinero real.

Lo que pidió el usuario el 28-10-2026: filtrar las noticias que aciertan ≥ 60 % de forma constante (bot/fiables.py),
confirmar la tendencia y entrar con los límites de siempre. Cuando se tría una noticia con dirección de un grupo FIABLE
(fiables.fiable_para con su medio, feed, tipo y sentido), para cada moneda a la que se aplica (fiables.monedas_de: la que
nombra el triaje o, si es una macro que la cripto confirmó según el reflejo, las cuatro):
1. Espera confirmación: que el precio de la moneda se mueva a su favor al menos el umbral del reflejo
   (reflejo.umbral_de: el mayor de 0,5 % y 2σ de 60 min de las 24 h anteriores, de esa moneda) en los 60 min siguientes
   al triaje. Referencia: el cierre de 1 min en ta = primer cierre tras el triaje (como acc60/acc240 del aprendizaje); se
   mide con los cierres de 1 min de Binance al contado (los de la vigilancia en memoria y la caché). Una confirmación que
   se ve más de LATE_MS después de su vela ya no se persigue (caduca).
2. Si se confirma, entra en ese momento por el MISMO camino que el motor de aperturas: evento «strategy» → control de
   riesgo → aprobación → paper (submit del PaperBroker), con horquilla pura desde el precio real de OKX EU: stop 1,5 %,
   objetivo 3R (+4,5 %), como mucho 24 h, largos al contado y cortos en perpetuo, riesgo config.RISK_PER_TRADE_PCT,
   origen «noticia_fiable» y la story de la noticia (en el ticket y en sus etiquetas).
3. Respeta: una posición por moneda (ni otra abierta ni en curso, de este módulo, del motor o de la fase 2), el tope de 6
   entradas por día de Madrid y el 3 % de riesgo del día COMPARTIDOS con el motor (apertura.paper_day_entries: cuentan
   todas las entradas del paper del día), MAX_POSITIONS, el paper detenido y las reglas del usuario (stop ≥ 1,5 %,
   R:R ≥ 2, objetivo ≥ 3 % neto de costes y funding esperado, sin break-even ni stop dinámico), que se comprueban aquí
   aunque BOT_USER_RULES=0.
Sin grupos fiables que pasen la puerta para operar (fiables.PUERTA: azar que conserva la hora, noticias distintas, acierto
bruto y horquilla, y hacia delante con lo que usaría en vivo) no entra nunca (ni publica nada); el 28-10-2026 no pasa
ninguno. Mientras no haya, las triadas esperan en la cola hasta que ya no se puedan confirmar a tiempo (las del arranque
no se pierden si el primer cálculo de fiables llega después). BOT_ENTRADA_NOTICIAS=0 la apaga (por defecto encendida).

Publica eventos «entrada_noticia» (story = la de la noticia) con patch.entrada_noticia = {estado («esperando»,
«confirmada», «caducada» o «vetada»), motivo, moneda, dirección, grupo, umbral, movimiento…}. Al arrancar recupera las
triadas de los últimos RESTORE_MIN minutos sin estado final (las que ya no se pueden confirmar a tiempo caducan).
HONESTO: que una noticia sea de un grupo «fiable» con pocos días de datos no prueba ventaja; se mide en paper."""
import asyncio
import inspect
import logging
import os
import time
from collections import OrderedDict
from datetime import datetime

import numpy as np

from . import apertura, aprendizaje, config, fiables, paper, reflejo
from .models import now_ms

log = logging.getLogger("entrada_noticias")

MIN, H = 60_000, 3_600_000
ENABLED = os.environ.get("BOT_ENTRADA_NOTICIAS", "1") != "0"
TICK_S = 20
VENTANA_MIN = 60                 # minutos tras el triaje para que el precio confirme
LATE_MS = 3 * MIN                # una confirmación vista más tarde de esto ya no se persigue
MAX_DIA = 6                      # entradas por día de Madrid, compartidas con el motor (todas las del paper)
RIESGO_DIA_PCT = 3.0             # riesgo comprometido en stop por día de Madrid (0,5 % × 6), compartido
MIN_NET_PCT = 3.0                # regla del usuario: el objetivo deja ≥ 3 % neto de costes
PARAMS = {"sl_pct": 1.5, "target_r": 3.0, "max_hold_h": 24, "min_net_pct": MIN_NET_PCT, "avg_hold_h": 13,
          "long_instrument": "spot", "short_instrument": "perp"}
SRC = "Entrada por noticia fiable"
MODE = "A favor de la noticia"   # uno de claude_agents.MODES
ORIGEN = "noticia_fiable"
WINDOW = "Noticia fiable"
ENTRY_TYPE = "Confirmación del precio tras una noticia fiable (a mercado)"
ESTADOS = ("esperando", "confirmada", "caducada", "vetada")
FINALES = ("confirmada", "caducada", "vetada")
RESTORE_MIN = VENTANA_MIN + 15
CAP_KEEP_MS = 6 * H
CAP_MAX = 50_000
DONE_MAX = 20_000
ERR_LOG_S = 300


def _n(x: float, d: int = 2) -> str:
    return f"{x:.{d}f}".replace(".", ",")


def _sg(x: float, d: int = 2) -> str:
    return ("+" if x >= 0 else "") + _n(x, d)


def _hm(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, apertura.MAD).strftime("%H:%M")


def _f(x, default=None):
    try:
        return float(x) if x not in (None, "") else default
    except (TypeError, ValueError):
        return default


async def _maybe(x):
    return await x if inspect.isawaitable(x) else x


# ---------------------------------------------------------------- reglas (puras)

def ta_de(ts_triaje: int, t0: int | None = None) -> int:
    """Primer cierre de 1 min tras el triaje (y tras la captura): desde ahí se pudo usar la noticia."""
    return max(int(t0 or 0), int(ts_triaje)) // MIN * MIN + MIN


def confirmar(bars, ta: int, d: int, umbral: float, now: int, ventana_min: int = VENTANA_MIN) -> dict:
    """¿Confirmó el precio? Con las velas de 1 min de la moneda ([t, o, h, l, c, …]): referencia = el cierre de la vela
    que cierra en ta (como mucho reflejo.P0_TOL_MS antes); confirmada en el primer cierre de (ta, ta + ventana] que
    queda a favor ≥ umbral. Devuelve {estado: confirmada | esperando | caducada, mov_pct, min, t (cierre que confirma),
    ref}. Caduca pasada la ventana y LATE_MS sin confirmar (o sin velas para medir)."""
    end = ta + ventana_min * MIN
    sr = reflejo._serie(bars, ta, end) if bars is not None and len(bars) else None
    if sr is None:
        return {"estado": "caducada" if now >= end + LATE_MS else "esperando", "mov_pct": None, "min": None,
                "t": None, "ref": None}
    p0, ts, cs = sr
    mov = (cs / p0 - 1) * 100 if len(cs) else np.array([])
    hit = np.flatnonzero(d * mov >= umbral - 1e-12)
    if len(hit):
        k = int(hit[0])
        return {"estado": "confirmada", "mov_pct": round(float(mov[k]), 3), "min": int(round((ts[k] - ta) / MIN)),
                "t": int(ts[k]), "ref": p0}
    last = round(float(mov[-1]), 3) if len(mov) else 0.0
    done = now >= end + LATE_MS or (len(ts) and ts[-1] >= end and now >= end)
    return {"estado": "caducada" if done else "esperando", "mov_pct": last,
            "min": int(round((ts[-1] - ta) / MIN)) if len(ts) else 0, "t": None, "ref": p0}


def plan_de(d: int, entry: float, funding_pct: float | None = None) -> dict:
    """Horquilla pura desde la entrada real (apertura.impulse_plan con stop 1,5 %, 3R, 24 h y ≥ 3 % neto)."""
    p = apertura.impulse_params(PARAMS)[0]
    inst = p["long_instrument"] if d > 0 else p["short_instrument"]
    return apertura.impulse_plan(d, entry, p, funding_pct, inst)


def reglas_ko(plan: dict) -> str | None:
    """Reglas del usuario, aunque BOT_USER_RULES=0: stop ≥ 1,5 %, R:R ≥ 2, objetivo ≥ 3 % neto de costes y funding
    esperado, horquilla pura (sin break-even, stop dinámico ni parciales). Motivo o None."""
    if not plan.get("ok"):
        return plan.get("why") or "el plan no vale"
    if _f(plan.get("stopPct"), 0.0) < config.RULE_MIN_STOP_PCT - 1e-9:
        return f"regla del usuario: stop {_n(plan['stopPct'])} % < {_n(config.RULE_MIN_STOP_PCT)} %"
    if _f(plan.get("rr"), 0.0) < config.RULE_MIN_RR - 1e-9:
        return f"regla del usuario: R:R {_n(plan['rr'])} < {_n(config.RULE_MIN_RR)}"
    if _f(plan.get("net_pct"), 0.0) < MIN_NET_PCT - 1e-9:
        return f"regla del usuario: el objetivo deja {_n(plan['net_pct'])} % neto < {_n(MIN_NET_PCT)} %"
    if plan.get("beR") is not None or plan.get("trailPct") is not None or _f(plan.get("tp1Frac"), 0.0):
        return "regla del usuario: sin break-even, stop dinámico ni parciales"
    return None


def limites_dia(entradas: dict[str, float], nuevo: float) -> str | None:
    """Tope de 6 entradas y 3 % de riesgo por día de Madrid (compartidos con el motor). Motivo o None."""
    if len(entradas) >= MAX_DIA:
        return f"tope de {MAX_DIA} entradas del día alcanzado (compartido con el motor de aperturas, día de Madrid)"
    usado = sum(entradas.values())
    if usado + nuevo > RIESGO_DIA_PCT + 1e-9:
        return (f"riesgo del día agotado: {_n(usado)} % comprometido + {_n(nuevo)} % > {_n(RIESGO_DIA_PCT)} % "
                "(compartido con el motor)")
    return None


def _grupo_corto(g: dict) -> dict:
    return {k: g.get(k) for k in ("clave", "tipo", "nombre", "sentido", "n", "n_historias", "dias", "acierto",
                                  "acierto_bruto", "ic90_bajo", "opera")}


def _muestra(g: dict) -> str:
    """«N noticias» (distintas) o, en un cálculo sin ese campo, «N historias×moneda»."""
    nh = g.get("n_historias")
    return f"{nh} noticias distintas" if isinstance(nh, int) else f"{g.get('n')} historias×moneda"


def umbral_de(arr, coin: str, ta: int) -> tuple[float, bool]:
    """(umbral en %, fijo): el mayor de 0,5 % y 2σ de 60 min de las 24 h antes de ta (reflejo.umbral_de, de esa
    moneda). Sin σ porque las velas aún no llegan 24 h atrás, 0,5 % provisional (fijo False: se vuelve a calcular en la
    ronda siguiente); sin σ con las velas ya ahí (huecos), 0,5 % fijo: no la va a haber."""
    sg = reflejo.sigma60({coin: arr}, ta, [coin])
    if sg is not None:
        return round(max(reflejo.UMBRAL_PCT, reflejo.K_SIGMA * sg), 3), True
    cubre = arr is not None and len(arr) and float(arr[0][0]) <= ta - reflejo.SIGMA_H * H + reflejo.P0_TOL_MS
    return reflejo.UMBRAL_PCT, bool(cubre)


# ---------------------------------------------------------------- módulo

class EntradaNoticias:
    """Recibe captadas, triadas y reflejos por el gancho del bus (observe) y, cada TICK_S, comprueba las candidatas con
    las velas de bars(moneda, desde_ms, hasta_ms) y entra en el paper con submit(ticket, story, titular, estrategia,
    ventana) cuando el precio confirma. get_portfolio() da la cartera del paper (contrato 2.5)."""

    def __init__(self, bus, store, submit, get_portfolio, bars=None, clock=now_ms, enabled: bool | None = None):
        self.bus, self.store, self.submit, self.get_portfolio = bus, store, submit, get_portfolio
        self.bars, self.clock = bars, clock
        self.enabled = ENABLED if enabled is None else bool(enabled)
        self.captured: OrderedDict[str, dict] = OrderedDict()   # historia → medio, feed, clase, estado de la fuente
        self.queue: list[tuple] = []                            # triadas por mirar (story, título, triaje, ts)
        self.seen: OrderedDict[str, bool] = OrderedDict()        # historias ya miradas (no se repiten)
        self.waiting: dict[str, dict] = {}                       # historias que esperan al reflejo (macro)
        self.reflejos: OrderedDict[str, dict] = OrderedDict()    # último reflejo de cada historia
        self.cands: dict[tuple, dict] = {}                        # (story, moneda) → candidata esperando al precio
        self.done: OrderedDict[tuple, str] = OrderedDict()        # (story, moneda) → estado final
        self.sent: set[tuple] = set()                             # (story, moneda) con «esperando» ya publicado
        self.own: dict = {"date": None, "entries": {}}            # entradas enviadas hoy (el paper aún puede no verlas)
        self.counts: dict[str, int] = {e: 0 for e in ESTADOS}
        self._restored = False
        self._err_at: dict[str, float] = {}

    def _log_err(self, key: str, msg: str, *args, exc: bool = False) -> None:
        t = time.monotonic()
        if t - self._err_at.get(key, float("-inf")) >= ERR_LOG_S:
            self._err_at[key] = t
            (log.exception if exc else log.warning)(msg, *args)

    # ---- entrada de eventos (gancho síncrono del bus: nunca lanza ni publica)

    def observe(self, msg: dict) -> None:
        if not self.enabled:
            return
        try:
            if not isinstance(msg, dict) or msg.get("type") != "event":
                return
            kind, sid = msg.get("kind"), msg.get("story")
            if not sid:
                return
            p = msg.get("patch") or {}
            if kind == "captured":
                self.captured[sid] = {"outlet": aprendizaje.outlet_from_patch(p), "srcId": p.get("srcId"),
                                      "clase": p.get("clase") if isinstance(p.get("clase"), str) else None,
                                      "srcState": p.get("srcState"), "srcWeight": p.get("srcWeight"),
                                      "ts": int(msg.get("ts") or self.clock())}
                self.captured.move_to_end(sid)
                self._trim()
            elif kind in ("srcstate", "corroborated") and sid in self.captured and p.get("srcState"):
                self.captured[sid].update(srcState=p["srcState"], srcWeight=p.get("srcWeight"))
            elif kind == "triaged" and sid not in self.seen:
                self.seen[sid] = True
                while len(self.seen) > DONE_MAX:
                    self.seen.popitem(last=False)
                self.queue.append((sid, msg.get("title"), p.get("triage"), int(msg.get("ts") or self.clock())))
            elif kind == "reflejo" and isinstance(p.get("reflejo"), dict):
                self._reflejo(sid, p["reflejo"])
        except Exception:
            self._log_err("observe", "Entrada por noticia: no pude apuntar un evento", exc=True)

    def _reflejo(self, sid: str, r: dict) -> None:
        self.reflejos[sid] = r
        self.reflejos.move_to_end(sid)
        while len(self.reflejos) > DONE_MAX:
            self.reflejos.popitem(last=False)

    def _trim(self) -> None:
        now = self.clock()
        while self.captured and (len(self.captured) > CAP_MAX
                                 or now - next(iter(self.captured.values()))["ts"] > CAP_KEEP_MS):
            self.captured.popitem(last=False)

    # ---- eventos para la web

    def _event(self, story: str, coin: str | None, d: int, estado: str, motivo: str, extra: dict | None = None,
               now: int | None = None) -> None:
        now = self.clock() if now is None else now
        lado = "largo" if d > 0 else "corto"
        body = {"estado": estado, "motivo": motivo, "moneda": coin, "dir": d, "lado": lado, **(extra or {})}
        quien = coin or "las cuatro monedas"
        title = {"esperando": f"Noticia fiable: espero confirmación del precio en {quien}",
                 "confirmada": f"Noticia fiable confirmada: {'compra' if d > 0 else 'venta'} de {coin} (paper)",
                 "caducada": f"Noticia fiable sin confirmación en {quien}",
                 "vetada": f"Noticia fiable: no se entra en {quien}"}[estado]
        self.bus.publish({"type": "event", "ts": now, "kind": "entrada_noticia", "agent": "strat", "story": story,
                          "title": title, "coins": [coin] if coin else [], "text": apertura._cut(motivo, 400),
                          "patch": {"entrada_noticia": body}})
        if estado in FINALES:
            self.counts[estado] += 1
            self.done[(story, coin)] = estado
            while len(self.done) > DONE_MAX:
                self.done.popitem(last=False)

    # ---- de triada a candidata

    def _info(self, sids: list[str]) -> dict[str, dict]:
        """Medio, feed, clase y estado de la fuente de cada historia: de memoria o, si no están, de la base."""
        out = {s: self.captured[s] for s in sids if s in self.captured}
        miss = [s for s in sids if s not in out]
        if miss:
            try:
                out.update(apertura.story_sources(self.store, miss))
            except Exception as e:
                self._log_err("db", "Entrada por noticia: no pude leer el medio de %d noticias: %s", len(miss), e)
        return out

    def _triadas(self, now: int) -> None:
        if not fiables.hay_fiables():   # sin grupos que operan no se hace nada (ni se publica): esperan en la cola
            self.queue = [x for x in self.queue if now < ta_de(x[3]) + VENTANA_MIN * MIN + LATE_MS]
            return
        q, self.queue = self.queue, []
        if not q:
            return
        info = self._info([x[0] for x in q])
        for sid, title, tr, ts in q:
            try:
                self._triada(sid, title, tr or {}, ts, info.get(sid) or {}, now)
            except Exception:
                self._log_err("triada", "Entrada por noticia: no pude mirar %s", sid, exc=True)

    def _triada(self, sid: str, title, tr: dict, ts: int, src: dict, now: int) -> None:
        d = reflejo.direccion(tr)
        imp = _f(tr.get("importancia"))
        if not d or (imp is not None and imp < config.TRIAGE_MIN):
            return
        if src.get("srcState") in ("silenciada", "retirada") or _f(src.get("srcWeight"), 1.0) <= 0:
            return
        g = fiables.fiable_para(src.get("outlet"), src.get("srcId"), tr.get("tipo"), d)
        if g is None:
            return
        ta = ta_de(ts)
        if now >= ta + VENTANA_MIN * MIN + LATE_MS:
            return   # recuperada al arrancar, pero ya no se puede confirmar a tiempo
        w = {"story": sid, "title": title or tr.get("resumen") or "", "triage": tr, "clase": src.get("clase"),
             "dir": d, "grupo": _grupo_corto(g), "ta": ta, "ts_triaje": ts}
        self._repartir(w, now)

    def _repartir(self, w: dict, now: int) -> None:
        """Monedas de la noticia (fiables.monedas_de): candidatas, espera del reflejo o vetada."""
        sid, d = w["story"], w["dir"]
        coins, why = fiables.monedas_de(w["title"], w["triage"], w["clase"], self.reflejos.get(sid))
        if coins:
            self.waiting.pop(sid, None)
            for c in coins:
                key = (sid, c)
                if key in self.cands or key in self.done:
                    continue
                self.cands[key] = {**w, "coin": c, "umbral": None, "umbral_fijo": False, "why_coins": why}
                if key not in self.sent:
                    self.sent.add(key)
                    self._event(sid, c, d, "esperando",
                                f"Noticia {'alcista' if d > 0 else 'bajista'} de un grupo fiable "
                                f"({w['grupo']['nombre']}, acierto {_n((w['grupo']['acierto'] or 0) * 100, 0)} % en "
                                f"{_muestra(w['grupo'])}): espero que {c} se mueva a su favor al menos el umbral "
                                f"del reflejo (el mayor de 0,5 % y 2σ de 60 min) antes de las "
                                f"{_hm(w['ta'] + VENTANA_MIN * MIN)} Madrid",
                                {"grupo": w["grupo"], "ta": w["ta"], "expira": w["ta"] + VENTANA_MIN * MIN,
                                 "ts_triaje": w["ts_triaje"]}, now)
            return
        if why == "esperando_reflejo":
            if now >= w["ta"] + VENTANA_MIN * MIN + LATE_MS:
                self.waiting.pop(sid, None)
                if (sid, None) not in self.done:
                    self._event(sid, None, d, "caducada", "La cripto no confirmó la noticia a tiempo (reflejo sin "
                                                          "estado final en 60 min): no se entra",
                                {"grupo": w["grupo"], "ta": w["ta"]}, now)
            else:
                self.waiting[sid] = w
            return
        self.waiting.pop(sid, None)
        if (sid, None) not in self.done:
            self._event(sid, None, d, "vetada", f"Noticia de un grupo fiable ({w['grupo']['nombre']}), pero {why}: "
                                                "no se entra", {"grupo": w["grupo"], "ta": w["ta"]}, now)

    # ---- velas y confirmación

    def _bars(self, coins: list[str], a: int, b: int) -> dict:
        out = {}
        for c in coins:
            try:
                x = self.bars(c, a, b) if self.bars is not None else None
                out[c] = np.asarray(x, float) if x is not None and len(x) else None
            except Exception as e:
                self._log_err("bars:" + c, "Entrada por noticia: sin velas de %s: %s", c, e)
                out[c] = None
        return out

    async def step(self, now: int | None = None) -> int:
        """Una ronda: triadas nuevas, esperas del reflejo y candidatas frente al precio. Devuelve las entradas hechas."""
        if not self.enabled:
            return 0
        now = self.clock() if now is None else int(now)
        if not self._restored:
            self._restored = True
            self.restore(now)
        self._triadas(now)
        for w in list(self.waiting.values()):
            self._repartir(w, now)
        if not self.cands:
            return 0
        coins = sorted({c["coin"] for c in self.cands.values()})
        a = min(c["ta"] for c in self.cands.values()) - reflejo.SIGMA_H * H - 2 * reflejo.P0_TOL_MS
        bars = await asyncio.to_thread(self._bars, coins, a, now + MIN)
        entered = 0
        for key, c in list(self.cands.items()):
            try:
                arr = bars.get(c["coin"])
                if not c.get("umbral_fijo") and arr is not None:   # hasta que haya σ (o no pueda haberla)
                    c["umbral"], c["umbral_fijo"] = umbral_de(arr, c["coin"], c["ta"])
                r = confirmar(arr, c["ta"], c["dir"], c["umbral"] or reflejo.UMBRAL_PCT, now)
                if r["estado"] == "esperando":
                    continue
                del self.cands[key]
                extra = {"grupo": c["grupo"], "ta": c["ta"], "umbral_pct": c["umbral"], "mov_pct": r["mov_pct"],
                         "min": r["min"]}
                if r["estado"] == "caducada":
                    mov = f" ({_sg(r['mov_pct'])} % al final)" if r["mov_pct"] is not None else " (sin velas)"
                    self._event(c["story"], c["coin"], c["dir"], "caducada",
                                f"{c['coin']} no se movió a favor ≥ {_n(c['umbral'] or reflejo.UMBRAL_PCT)} % en los "
                                f"{VENTANA_MIN} min siguientes al triaje{mov}: no se entra", extra, now)
                elif now - r["t"] > LATE_MS:
                    self._event(c["story"], c["coin"], c["dir"], "caducada",
                                f"Se confirmó a las {_hm(r['t'])} ({_sg(r['mov_pct'])} % en {r['min']} min), pero las "
                                "velas llegaron tarde: no se persigue el precio", extra, now)
                else:
                    entered += await self._entrar(c, r, extra, now)
            except Exception:
                self._log_err("medida", "Entrada por noticia: no pude comprobar %s", key, exc=True)
        return entered

    # ---- entrada en el paper (el mismo camino que el motor)

    def _own_today(self, now: int) -> dict:
        today = datetime.fromtimestamp(now / 1000, apertura.MAD).date().isoformat()
        if self.own["date"] != today:
            self.own = {"date": today, "entries": {}}
        return self.own["entries"]

    def _live(self, coin: str, now: int) -> float | None:
        m = (getattr(self.bus, "state", None) or {}).get("market") or {}
        live, upd = _f((m.get("prices") or {}).get(coin)), _f((m.get("updated") or {}).get(coin))
        return live if live and (upd is None or now - upd <= 15_000) else None

    def _funding(self, coin: str) -> float | None:
        m = (getattr(self.bus, "state", None) or {}).get("market") or {}
        return paper.funding_8h((m.get("flow") or {}).get(coin))

    async def _entrar(self, c: dict, r: dict, extra: dict, now: int) -> int:
        story, coin, d = c["story"], c["coin"], c["dir"]
        conf = (f"{coin} {_sg(r['mov_pct'])} % a los {r['min']} min del triaje (umbral "
                f"{_n(c['umbral'] or reflejo.UMBRAL_PCT)} %)")

        def veto(why: str) -> int:
            self._event(story, coin, d, "vetada", f"Confirmada ({conf}), pero {why}: no se entra", extra, now)
            return 0

        try:
            pf = await _maybe(self.get_portfolio())
        except Exception as e:
            self._log_err("pf", "Entrada por noticia: cartera no disponible: %s", e)
            pf = None
        if not isinstance(pf, dict):
            return veto("sin acceso a la cartera del paper")
        if pf.get("halted") or (pf.get("limits") or {}).get("halted"):
            return veto("el paper está detenido")
        busy = apertura.OpeningEngine._coin_busy(pf, coin)
        if busy:
            return veto(f"{busy}: una sola posición por moneda")
        n_open = sum(len(pf.get(k) or []) for k in ("positions", "pending", "scheduled"))
        if n_open >= config.MAX_POSITIONS:
            return veto(f"ya hay {n_open} operaciones abiertas o en curso (máximo {config.MAX_POSITIONS})")
        risk = float(config.RISK_PER_TRADE_PCT)
        entradas = {**self._own_today(now), **apertura.paper_day_entries(pf, now)}
        lim = limites_dia(entradas, risk)
        if lim:
            return veto(lim)
        entry = self._live(coin, now)
        if entry is None:
            return veto(f"sin precio reciente de {coin} en OKX EU")
        plan = plan_de(d, entry, self._funding(coin))
        why = reglas_ko(plan)
        if why:
            return veto(why)
        inst = plan["instrument"]
        verb = "Compra" if d > 0 else "Venta"
        end = now + int(PARAMS["max_hold_h"] * H)
        g = c["grupo"]
        tags = {"origen": ORIGEN, "story": story, "grupo": g["clave"], "grupo_nombre": g["nombre"],
                "grupo_tipo": g["tipo"], "acierto": g["acierto"], "ic90_bajo": g["ic90_bajo"],
                "umbral_pct": c["umbral"], "mov_pct": r["mov_pct"], "min_confirmacion": r["min"],
                "lado": "largo" if d > 0 else "corto", "instrumento": inst}
        story_p = f"nf-{story}-{coin.lower()}"
        why_t = (f"Noticia {'alcista' if d > 0 else 'bajista'} de un grupo fiable ({g['nombre']}, acierto "
                 f"{_n((g['acierto'] or 0) * 100, 0)} %) confirmada por el precio: {conf}")
        t = {"coin": coin, "side": "long" if d > 0 else "short", "mode": MODE, "risk": risk,
             "stopPct": plan["stopPct"], "trailPct": None, "timing": "now", "win": WINDOW, "winAt": None,
             "entryType": ENTRY_TYPE, "tp1R": plan["tp2R"], "tp2R": plan["tp2R"], "tp1Frac": 0.0, "beR": None,
             "trailStartR": None,
             "valid": f"como mucho {_n(PARAMS['max_hold_h'], 0)} h (hasta el "
                      f"{datetime.fromtimestamp(end / 1000, apertura.MAD).strftime('%d-%m %H:%M')} Madrid)",
             "inval": apertura._cut(f"Stop {apertura._px(plan['stop_px'])} (−{_n(plan['stopPct'])} %) u objetivo "
                                    f"{apertura._px(plan['target_px'])} (+{_n(plan['gross_pct'])} %)", 160),
             "why": apertura._cut(why_t, 300), "llm": None, "winEnd": end,
             "holdLabel": f"tope de {_n(PARAMS['max_hold_h'], 0)} h", "maxAgeMs": apertura.MAX_FILL_DELAY_MS,
             "instrument": inst, "market": None, "src": SRC, "signal": {"dir": d, "strength": None},
             "origen": ORIGEN, "noticia": story, "strategy": f"noticia-fiable-{'largo' if d > 0 else 'corto'}",
             "tags": tags}
        inst_t = "perpetuo" if inst == "perp" else "contado"
        hl = (f"Noticia fiable · {verb} {coin} ({inst_t}, paper) · {g['nombre']} · {_sg(r['mov_pct'])} % en "
              f"{r['min']} min")
        try:
            self.bus.publish({"type": "event", "ts": now, "kind": "strategy", "agent": "strat", "story": story_p,
                              "title": f"{verb} {coin} · Noticia fiable confirmada (paper)", "coins": [coin],
                              "text": apertura._cut(f"{why_t} · {plan['why']} · riesgo {_n(risk)} %", 400),
                              "patch": {"headline": hl, "src": SRC, "tier": "Plan", "t0": now, "coins": [coin],
                                        "strat": t, "tags": dict(tags), "noticia": story}})
            ok = await _maybe(self.submit(t, story_p, hl, t["strategy"], WINDOW))
        except Exception as e:
            log.exception("Entrada por noticia: submit de %s falló: %s", story_p, e)
            ok = False
        if not ok:
            return veto("vetada por el control de riesgo del paper")
        self._own_today(now)[story_p] = risk
        self._event(story, coin, d, "confirmada",
                    f"{why_t} · {verb.lower()} {apertura.INST_TEXT[inst]} a {apertura._px(entry)}, stop "
                    f"{apertura._px(plan['stop_px'])} y objetivo {apertura._px(plan['target_px'])} (+"
                    f"{_n(plan['gross_pct'])} %, {_n(plan['net_pct'])} % neto), como mucho "
                    f"{_n(PARAMS['max_hold_h'], 0)} h",
                    {**extra, "paper_story": story_p, "entrada": entry, "stop": plan["stop_px"],
                     "objetivo": plan["target_px"], "instrumento": inst, "riesgo_pct": risk}, now)
        log.info("Entrada por noticia: %s %s (%s, grupo %s)", verb, coin, story_p, g["clave"])
        return 1

    # ---- arranque

    def restore(self, now: int) -> int:
        """Al arrancar: triadas y reflejos de los últimos RESTORE_MIN minutos y los estados ya publicados (lo final no
        se repite; lo que ya no se puede confirmar a tiempo caduca o se descarta)."""
        if self.store is None:
            return 0
        try:
            evs = self.store.events_since(now - RESTORE_MIN * MIN, ("triaged", "reflejo", "entrada_noticia"))
        except Exception as e:
            self._log_err("restore", "Entrada por noticia: no pude recuperar lo pendiente: %s", e)
            return 0
        n = 0
        for e in evs:
            sid, p = e.get("story"), e.get("patch") or {}
            if not sid:
                continue
            if e.get("kind") == "entrada_noticia":
                en = p.get("entrada_noticia") or {}
                key = (sid, en.get("moneda"))
                if en.get("estado") in FINALES:
                    self.done[key] = en["estado"]
                elif en.get("estado") == "esperando":
                    self.sent.add(key)
            elif e.get("kind") == "reflejo" and isinstance(p.get("reflejo"), dict):
                self._reflejo(sid, p["reflejo"])
        for e in evs:
            sid = e.get("story")
            if e.get("kind") == "triaged" and sid and sid not in self.seen:
                self.seen[sid] = True
                self.queue.append((sid, e.get("title"), (e.get("patch") or {}).get("triage"), int(e.get("ts") or now)))
                n += 1
        return n

    async def run(self) -> None:
        while True:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._log_err("ronda", "Entrada por noticia: fallo en la ronda; sigue en la próxima", exc=True)
            await asyncio.sleep(TICK_S)

    def view(self) -> dict:
        return {"encendida": self.enabled, "hay_fiables": fiables.hay_fiables(), "esperando": len(self.cands),
                "esperando_reflejo": len(self.waiting), "finales": dict(self.counts)}
