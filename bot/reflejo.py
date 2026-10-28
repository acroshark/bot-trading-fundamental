"""Reflejo en la cripto de las noticias macro y geopolíticas (contrato 2.6, evento «reflejo»).

Lo que pidió el usuario: le interesan las noticias de cripto y, de las demás (guerras, sanciones, aranceles, bancos
centrales, datos macro, crisis…), solo las que se ven reflejadas en el mercado. Para cada historia que NO es de cripto y
que el triaje dejó con dirección (alcista o bajista), se mide la cripto desde su captura:
- «confirmada»: en los 60 min siguientes la cripto se movió a favor de la dirección de la noticia al menos el umbral;
- «contraria»: se movió al menos el umbral en contra;
- «sin_reflejo»: pasados 60 min, ninguna de las dos;
- «pendiente»: aún dentro de los 60 min.
Manda lo que pase antes (primer toque). La medida es la media de las cuatro monedas (cierre de cada vela de 1 min frente
al cierre de la última vela cerrada antes de la captura) o, si falta alguna, BTC sola.

Umbral (28-10-2026): el mayor de 0,5 % y 2σ del movimiento de 60 min de esa misma referencia en las 24 h anteriores a la
captura (umbral_pct en el patch). Con el 0,5 % fijo, un momento cualquiera con una dirección al azar salía «confirmada»
una de cada cinco veces (y «contraria» otra de cada cinco), y en una hora de caída del 0,5 % se confirmaban todas las
bajistas a la vez. Con max(0,5 %, 2σ) el azar baja a una de cada veinte (AZAR, medido con este mismo módulo y las velas
de 1 min de junio a octubre de 2026). Y con las macro reales de 5 días la dirección del triaje no se cumple más que al
azar: una «confirmada» no prueba causalidad (docs/05, sección 18).

Solo las «confirmada» cuentan en la señal del motor (apertura._news y signal_getter), en la balanza de la vista Mundo, en
el contexto del estratega (claude_agents) y en las predicciones; en /api/noticias las demás salen con credibilidad
ajustada 0. Las noticias de cripto cuentan como siempre (cuenta()).

¿Qué es macro? La clase del prefiltro de su captada (patch.clase de filters.classify: «cripto», «macro» o
«contexto»). Sin clase (historias de antes del 28-10-2026), solo el titular: que nombre una de las cuatro monedas o hable
de cripto. Nunca los temas ni el tipo del triaje: la IA local pone el tema «cripto» o el tipo «seguridad» a noticias de la
Fed o de guerra (el 12 % de las macro con dirección del 27-10-2026 se habría colado así).

Velas: las de la vigilancia en memoria más la caché de HIST_DIR/1m (aprendizaje.CacheBars), como el aprendizaje; los
precios son los de Binance al contado que también usa el motor. Si falta todo durante 3 h, la historia queda «sin_reflejo»
sin medida. Al arrancar se recuperan las triadas de las últimas RESTORE_H horas (la señal del motor y la balanza de la web
miran 6 h) que no tenían estado final: las que ya no se pueden medir quedan «sin_reflejo»."""
import asyncio
import logging
import time
from collections import Counter, OrderedDict

import numpy as np

from . import config, filters, topics
from .models import now_ms

log = logging.getLogger("reflejo")

MIN, H = 60_000, 3_600_000
COINS = list(config.COINS)
UMBRAL_PCT = 0.5            # movimiento mínimo de la cripto que confirma (o contradice) la noticia
K_SIGMA = 2.0               # ... y, si es mayor, 2σ del movimiento de 60 min en las 24 h anteriores a la captura
SIGMA_H = 24
SIGMA_MIN_N = 600           # rendimientos de 1 min que hacen falta para estimar σ (si no, solo el 0,5 %)
VENTANA_MIN = 60            # minutos que se mira desde la captura
DIR_MIN = 15                # |dirección| del triaje que ya es alcista o bajista (local_ai.sentiment_of)
P0_TOL_MS = 5 * MIN         # la vela de referencia tiene que cerrar como mucho 5 min antes de la captura
LATE_MS = 10 * MIN          # pasados los 60 min, se espera hasta 10 min más a que lleguen las velas
GIVE_UP_MS = 3 * H          # sin velas en 3 h: «sin_reflejo» sin medida
RESTORE_H = 7               # al arrancar, triadas de las últimas 7 h sin estado final (la señal y la web miran 6 h)
TICK_S = 20
KEEP_CAPTURED_MS = 6 * H
CAPTURED_MAX = 50_000
DONE_MAX = 20_000
ERR_LOG_S = 300
ESTADOS = ("pendiente", "confirmada", "contraria", "sin_reflejo")
FINAL = ("confirmada", "contraria", "sin_reflejo")
CLASES_NO_CRIPTO = ("macro", "contexto")   # clases del prefiltro (filters.CLASES) que no son de cripto
# Lo que saldría «confirmada» por puro azar (momento y dirección al azar; velas de 1 min de junio a octubre de 2026;
# medido con medir() el 28-10-2026). Lo enseñan la web y view() para comparar con la tasa de confirmación real.
AZAR = {"umbral_fijo_pct": 18.7, "umbral_2sigma_pct": 5.0, "casos": 6000, "periodo": "2026-06-02/2026-10-28"}


def _num(x: float, d: int = 2) -> str:
    return f"{x:.{d}f}".replace(".", ",")


def _sg(x: float, d: int = 2) -> str:
    return ("+" if x >= 0 else "") + _num(x, d)


# ---------------------------------------------------------------- reglas (puras)

def es_cripto(title: str | None, clase: str | None = None) -> bool:
    """¿Es una noticia de cripto? Manda la clase del prefiltro de su captada (patch.clase). Sin clase (historias de antes
    del 28-10-2026), solo el titular: nombra una de las cuatro monedas o habla de cripto (las mismas reglas que
    filters.classify). Nunca el tipo ni los temas del triaje."""
    if clase == "cripto":
        return True
    if clase in CLASES_NO_CRIPTO:
        return False
    return bool(title) and bool(filters.detect_coins(title) or filters.CRYPTO_RX.search(title)
                                or "cripto" in topics.topics(title))


def direccion(triage: dict | None) -> int:
    """+1 alcista, −1 bajista, 0 neutral (|dirección| ≤ 15, como el sentimiento del triaje)."""
    rep = (triage or {}).get("repercusion") if isinstance(triage, dict) else None
    try:
        d = float((rep or {}).get("direccion") or 0)
    except (TypeError, ValueError):
        d = 0.0
    return 1 if d > DIR_MIN else -1 if d < -DIR_MIN else 0


def aplica(title: str | None, triage: dict | None, clase: str | None = None) -> bool:
    """¿Necesita reflejo? Noticia macro o geopolítica (no de cripto) con dirección."""
    return direccion(triage) != 0 and not es_cripto(title, clase)


def cuenta(title: str | None, triage: dict | None, reflejo: dict | None, clase: str | None = None) -> bool:
    """¿Cuenta en la señal? Las de cripto (y las neutrales) como siempre; las macro solo si la cripto las confirmó.
    clase: la del prefiltro de su captada (patch.clase); sin ella, se decide por el titular."""
    if not aplica(title, triage, clase):
        return True
    return isinstance(reflejo, dict) and reflejo.get("estado") == "confirmada"


def macro_confirmada(title: str | None, triage: dict | None, reflejo: dict | None, clase: str | None = None) -> bool:
    """Macro o geopolítica (clase «macro» del prefiltro) con dirección que la cripto confirmó: se midió con la media de
    las cuatro monedas (o BTC), así que vale para todas, como una global. Sin clase (antes del 28-10-2026) no: entonces
    la mesa ya le ponía las cuatro monedas."""
    return (clase == "macro" and aplica(title, triage, clase) and isinstance(reflejo, dict)
            and reflejo.get("estado") == "confirmada")


def clases(store, stories) -> dict[str, str | None]:
    """Clase del prefiltro (patch.clase de la captada) de cada historia, por el índice de story. store: un Store o una
    conexión sqlite3. Sin base (pruebas) o sin captada, la historia no sale; con captada sin clase (antes del
    28-10-2026), None."""
    db = store if hasattr(store, "execute") else getattr(store, "db", None)
    ids = list(dict.fromkeys(s for s in stories if s))
    out: dict[str, str | None] = {}
    if db is None or not ids:
        return out
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        rows = db.execute(f"SELECT story, json_extract(body, '$.patch.clase') FROM events WHERE kind='captured' AND "
                          f"story IN ({','.join('?' * len(part))}) ORDER BY ts, rowid", part).fetchall()
        for story, clase in rows:
            out.setdefault(story, clase)
    return out


def _serie(a, t0: int, end: int):
    """(precio de referencia, horas de cierre y cierres de las velas que cierran en (t0, end]) de una moneda."""
    a = np.asarray(a, float)
    if not len(a):
        return None
    close_t = a[:, 0] + MIN
    k = int(np.searchsorted(close_t, t0, "right")) - 1
    if k < 0 or t0 - close_t[k] > P0_TOL_MS or not a[k, 4] > 0:
        return None
    sel = (close_t > t0) & (close_t <= end)
    return float(a[k, 4]), close_t[sel], a[sel, 4]


def sigma60(bars: dict, t0: int, use) -> float | None:
    """σ (en %) del movimiento de 60 min de la media de `use` con los rendimientos de 1 min de las SIGMA_H horas antes de
    t0 (velas que cierran antes de la captura; solo minutos seguidos). None con menos de SIGMA_MIN_N."""
    sers = []
    for c in use:
        a = bars.get(c)
        if a is None or not len(a):
            return None
        a = np.asarray(a, float)
        sel = (a[:, 0] >= t0 - SIGMA_H * H) & (a[:, 0] + MIN <= t0) & (a[:, 4] > 0)
        sers.append((a[sel, 0], a[sel, 4]))
    t = sers[0][0]
    for ts, _ in sers[1:]:
        t = np.intersect1d(t, ts)
    if len(t) < SIGMA_MIN_N + 1:
        return None
    seguido = np.diff(t) == MIN
    r = np.zeros(int(seguido.sum()))
    for ts, cs in sers:
        lc = np.log(cs[np.searchsorted(ts, t)])
        r += np.diff(lc)[seguido]
    r /= len(sers)
    if len(r) < SIGMA_MIN_N:
        return None
    return float(np.std(r) * np.sqrt(60) * 100)


def umbral_de(bars: dict, t0: int, use) -> float:
    """El mayor de UMBRAL_PCT y K_SIGMA·σ60 (sin σ, UMBRAL_PCT)."""
    sg = sigma60(bars, t0, use)
    return max(UMBRAL_PCT, K_SIGMA * sg) if sg else UMBRAL_PCT


def medir(bars: dict, t0: int, d: int, now: int, umbral: float | None = None,
          ventana_min: int = VENTANA_MIN) -> dict | None:
    """Estado del reflejo de una noticia de dirección d capturada en t0, con las velas de 1 min de cada moneda
    ([t, o, h, l, c, …]). umbral None: el mayor de 0,5 % y 2σ de 60 min de las 24 h anteriores (umbral_de). None si aún
    no hay con qué medir (ni las cuatro monedas ni BTC tienen precio de referencia)."""
    end = t0 + ventana_min * MIN
    series = {}
    for c in COINS:
        a = bars.get(c)
        sr = _serie(a, t0, end) if a is not None and len(a) else None
        if sr:
            series[c] = sr
    if all(c in series for c in COINS):
        ref, use = "media", COINS
    elif "BTC" in series:
        ref, use = "BTC", ["BTC"]
    else:
        return None
    if umbral is None:
        umbral = umbral_de(bars, t0, use)
    base = {"ref": ref, "umbral_pct": round(float(umbral), 3)}
    last = [float(series[c][1][-1]) if len(series[c][1]) else float(t0) for c in use]
    common = min(last)   # hasta donde hay velas de todas las que cuentan
    times = [series[c][1] for c in use if len(series[c][1])]
    grid = np.unique(np.concatenate(times)) if times else np.array([])
    grid = grid[grid <= common]
    complete = common >= end // MIN * MIN
    if not len(grid):
        if now >= end + LATE_MS:
            return {"estado": "sin_reflejo", "mov_pct": 0.0, "min": 0, **base}
        return {"estado": "pendiente", "mov_pct": 0.0, "min": 0, **base}
    moves = np.zeros(len(grid))
    for c in use:
        p0, ts, cs = series[c]
        k = np.searchsorted(ts, grid, "right") - 1
        px = np.where(k >= 0, cs[np.clip(k, 0, None)] if len(cs) else p0, p0)
        moves += (px / p0 - 1) * 100
    moves /= len(use)
    fav = d * moves
    hit = np.flatnonzero((fav >= umbral - 1e-12) | (fav <= -umbral + 1e-12))
    if len(hit):
        k = int(hit[0])
        estado = "confirmada" if fav[k] > 0 else "contraria"
        return {"estado": estado, "mov_pct": round(float(moves[k]), 3), "min": int(round((grid[k] - t0) / MIN)), **base}
    out = {"mov_pct": round(float(moves[-1]), 3), "min": min(ventana_min, int(round((grid[-1] - t0) / MIN))), **base}
    return {"estado": "sin_reflejo" if complete or now >= end + LATE_MS else "pendiente", **out}


def texto(r: dict, d: int) -> str:
    """Explicación en español del estado del reflejo."""
    sentido = "alcista" if d > 0 else "bajista"
    quien = "media de las cuatro monedas" if r.get("ref") == "media" else "BTC"
    mov = r.get("mov_pct")
    est = r.get("estado")
    u = r.get("umbral_pct")
    umbral = f"{_num(u, 2)} %" if isinstance(u, (int, float)) else f"{_num(UMBRAL_PCT, 1)} %"
    az = AZAR.get("umbral_2sigma_pct")
    azar = (f" Por puro azar se confirmaría alrededor del {_num(az, 0)} % de las noticias: no prueba que esta lo causara."
            if isinstance(az, (int, float)) else " Puede ser casualidad: no prueba que la noticia lo causara.")
    if est == "pendiente":
        return (f"Noticia {sentido} que no es de cripto: se mide si la cripto la refleja (el mayor de "
                f"{_num(UMBRAL_PCT, 1)} % y 2σ de 60 min, en {VENTANA_MIN} min). Mientras, no cuenta en la señal.")
    if mov is None:
        return "Sin velas de la cripto para medirla: queda sin reflejo y no cuenta en la señal."
    if est == "confirmada":
        return (f"La cripto la confirma: {_sg(mov)} % ({quien}) a los {r.get('min')} min, a favor de la noticia "
                f"{sentido} (umbral {umbral}). Cuenta en la señal." + azar)
    if est == "contraria":
        return (f"La cripto va en contra: {_sg(mov)} % ({quien}) a los {r.get('min')} min frente a una noticia "
                f"{sentido} (umbral {umbral}). No cuenta en la señal.")
    return (f"Sin reflejo en la cripto: {_sg(mov)} % ({quien}) en {VENTANA_MIN} min, menos del umbral de "
            f"{umbral}. No cuenta en la señal.")


# ---------------------------------------------------------------- módulo

class Reflejo:
    """Recibe las captadas y las triadas por el gancho del bus (observe) y, cada TICK_S, mide las pendientes con las
    velas de bars(moneda, desde_ms, hasta_ms) y publica el evento «reflejo» (pendiente una vez, y el estado final).
    Decide si una triada es macro con la clase de su captada (patch.clase): la de memoria o, si no está, la de la base."""

    def __init__(self, bus, bars=None, clock=now_ms, store=None):
        self.bus, self.bars, self.clock = bus, bars, clock
        self.store = store if store is not None else getattr(bus, "store", None)
        self.pending: dict[str, dict] = {}
        # historia → (hora de la captada, clase del prefiltro); todas, también las de cripto (para saber que lo son)
        self.captured: OrderedDict[str, tuple[int, str | None]] = OrderedDict()
        self.done: OrderedDict[str, str] = OrderedDict()       # historia → estado final (no se repite)
        self.counts: Counter = Counter()
        self._restored = False
        self._err_at: dict[str, float] = {}

    def _log_err(self, key: str, msg: str, *args, exc: bool = False) -> None:
        t = time.monotonic()
        if t - self._err_at.get(key, float("-inf")) >= ERR_LOG_S:
            self._err_at[key] = t
            (log.exception if exc else log.warning)(msg, *args)

    # ---- entrada

    def observe(self, msg: dict) -> None:
        """Gancho síncrono del bus: apunta la hora y la clase de cada captada y deja pendiente cada triada que necesita
        reflejo. Nunca lanza ni publica (lo hace step)."""
        try:
            if not isinstance(msg, dict) or msg.get("type") != "event":
                return
            kind, sid = msg.get("kind"), msg.get("story")
            if not sid:
                return
            if kind == "captured":
                clase = (msg.get("patch") or {}).get("clase")
                self.captured[sid] = (int(msg.get("ts") or self.clock()), clase if isinstance(clase, str) else None)
                self.captured.move_to_end(sid)
                self._trim_captured()
            elif kind == "triaged":
                cap = self.captured.get(sid)
                self.add(sid, msg.get("title"), (msg.get("patch") or {}).get("triage"), int(msg.get("ts") or self.clock()),
                         clase=cap[1] if cap else None, clase_known=cap is not None)
        except Exception:
            self._log_err("observe", "Reflejo: no pude apuntar un evento", exc=True)

    def add(self, sid: str, title: str | None, triage: dict | None, ts: int, t0: int | None = None,
            clase: str | None = None, clase_known: bool = True) -> bool:
        """Deja pendiente una triada que necesita reflejo. clase_known False: la captada no está en memoria y su clase
        se comprueba en la base antes de medir (si resulta de cripto, se descarta sin publicar nada)."""
        if sid in self.pending or sid in self.done or not aplica(title, triage, clase):
            return False
        cap = self.captured.get(sid)
        self.pending[sid] = {"t0": t0 if t0 is not None else (cap[0] if cap else None), "d": direccion(triage),
                             "title": title or (triage or {}).get("resumen") or "", "ts": ts, "sent": False,
                             "triage": triage, "check": not clase_known, "umbral": None}
        return True

    def _trim_captured(self) -> None:
        now = self.clock()
        while self.captured and (len(self.captured) > CAPTURED_MAX
                                 or now - next(iter(self.captured.values()))[0] > KEEP_CAPTURED_MS):
            self.captured.popitem(last=False)

    def _capture_info(self, sid: str) -> tuple[int | None, str | None, bool]:
        """(hora, clase, si existe) de la captada de la historia (memoria o base)."""
        if sid in self.captured:
            ts, clase = self.captured[sid]
            return ts, clase, True
        db = getattr(self.store, "db", None)
        if db is None:
            return None, None, False
        try:
            row = db.execute("SELECT ts, json_extract(body, '$.patch.clase') FROM events WHERE story=? AND "
                             "kind='captured' ORDER BY ts LIMIT 1", (sid,)).fetchone()
        except Exception as e:
            self._log_err("db", "Reflejo: no pude leer la captada de %s: %s", sid, e)
            return None, None, False
        if not row:
            return None, None, False
        return (int(row[0]) if row[0] is not None else None), row[1], True

    def restore(self, now: int) -> int:
        """Al arrancar: las triadas de las últimas RESTORE_H horas que necesitan reflejo y no tienen estado final (las
        que ya no se puedan medir quedarán «sin_reflejo»)."""
        if self.store is None:
            return 0
        try:
            evs = self.store.events_since(now - RESTORE_H * H, ("triaged", "reflejo"))
            cls = clases(self.store, [e.get("story") for e in evs if e.get("kind") == "triaged"])
        except Exception as e:
            self._log_err("restore", "Reflejo: no pude recuperar lo pendiente: %s", e)
            return 0
        final = {e.get("story") for e in evs if e.get("kind") == "reflejo"
                 and ((e.get("patch") or {}).get("reflejo") or {}).get("estado") in FINAL}
        sent = {e.get("story") for e in evs if e.get("kind") == "reflejo"}
        for s in final:
            self._done(s, "recuperada")
        n = 0
        for e in evs:
            sid = e.get("story")
            if e.get("kind") == "triaged" and sid and sid not in final:
                if self.add(sid, e.get("title"), (e.get("patch") or {}).get("triage"), int(e.get("ts") or now),
                            clase=cls.get(sid)):
                    self.pending[sid]["sent"] = sid in sent
                    n += 1
        if n:
            log.info("Reflejo: %d noticias pendientes recuperadas", n)
        return n

    def _done(self, sid: str, estado: str) -> None:
        self.done[sid] = estado
        while len(self.done) > DONE_MAX:
            self.done.popitem(last=False)

    # ---- medida

    def _bars(self, a: int, b: int) -> dict:
        out = {}
        if self.bars is None:
            return out
        for c in COINS:
            try:
                x = self.bars(c, a, b)
                out[c] = np.asarray(x, float) if x is not None and len(x) else None
            except Exception as e:
                self._log_err("bars:" + c, "Reflejo: sin velas de %s: %s", c, e)
                out[c] = None
        return out

    def _publish(self, sid: str, p: dict, r: dict) -> None:
        rf = {"estado": r["estado"], "mov_pct": r.get("mov_pct"), "min": r.get("min"), "ref": r.get("ref"),
              "umbral_pct": r.get("umbral_pct")}
        self.bus.publish({"type": "event", "kind": "reflejo", "agent": "flow", "story": sid, "ts": self.clock(),
                          "title": p["title"],
                          "coins": [], "text": texto(rf, p["d"]), "patch": {"reflejo": rf}})

    def _resolve(self) -> None:
        """Hora de la captada de las pendientes que no la tienen y clase de las que llegaron sin captada en memoria."""
        for sid, p in list(self.pending.items()):
            if p["t0"] is not None and not p["check"]:
                continue
            ts, clase, found = self._capture_info(sid)
            if p["check"]:
                p["check"] = False
                if found and not aplica(p["title"], p["triage"], clase):   # resulta de cripto: no necesita reflejo
                    del self.pending[sid]
                    self._done(sid, "cripto")
                    continue
            if p["t0"] is None:
                p["t0"] = ts or p["ts"]

    async def step(self, now: int | None = None) -> int:
        """Mide las pendientes y publica lo que cambie. Devuelve cuántas quedaron con estado final."""
        now = self.clock() if now is None else int(now)
        if not self._restored:
            self._restored = True
            self.restore(now)
        self._resolve()
        if not self.pending:
            return 0
        a = min(p["t0"] - (0 if p["umbral"] is not None else SIGMA_H * H) for p in self.pending.values()) - 2 * P0_TOL_MS
        bars = await asyncio.to_thread(self._bars, a, now + MIN)
        closed = 0
        for sid, p in list(self.pending.items()):
            try:
                r = medir(bars, p["t0"], p["d"], now, umbral=p["umbral"])
                if r is None:
                    if now - p["t0"] < GIVE_UP_MS:
                        if not p["sent"]:
                            self._publish(sid, p, {"estado": "pendiente", "mov_pct": None, "min": 0, "ref": None})
                            p["sent"] = True
                        continue
                    r = {"estado": "sin_reflejo", "mov_pct": None, "min": 0, "ref": None}
                elif p["umbral"] is None:
                    p["umbral"] = r["umbral_pct"]   # el de la primera medida: el de las 24 h antes de la captura
                if r["estado"] in FINAL:
                    self._publish(sid, p, r)
                    self.counts[r["estado"]] += 1
                    self._done(sid, r["estado"])
                    del self.pending[sid]
                    closed += 1
                elif not p["sent"]:
                    self._publish(sid, p, r)
                    p["sent"] = True
            except Exception:
                self._log_err("medida", "Reflejo: no pude medir %s", sid, exc=True)
        return closed

    async def run(self) -> None:
        while True:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._log_err("ronda", "Reflejo: fallo en la ronda; sigue en la próxima", exc=True)
            await asyncio.sleep(TICK_S)

    def view(self) -> dict:
        """Recuento desde el arranque y lo que saldría «confirmada» por puro azar, para comparar."""
        fin = sum(self.counts[e] for e in FINAL)
        return {"pendientes": len(self.pending), "finales": dict(self.counts),
                "tasa_confirmada": round(self.counts["confirmada"] / fin, 4) if fin else None,
                "azar_confirmada_pct": AZAR.get("umbral_2sigma_pct")}
