"""Por qué entra o no en cada apertura: tabla «decisiones_apertura» (docs/04, 2.17 y §4). SOLO PAPER.

El motor de aperturas (modo impulso, el de en vivo) apunta aquí cada punto de decisión de cada apertura×moneda: al
empezar a medir (esperando), al terminar la medida (entra, o no entra y por qué), al enviar la entrada al paper o
descartarla (tope del día, moneda ocupada, reglas…), y al arrancar, las aperturas que pasaron con el bot parado. La
entrada por noticia fiable (bot/entrada_noticias.py) va igual, con la ventana «noticia» (ventana_id «noticia-<story>»).
Los rechazos del riesgo y del paper y la apertura y el cierre de la operación (con su R) llegan por el gancho del bus
(observe): eventos riskko, humanko, blocked, filled y closed de las stories que el motor (a-…) o la entrada por noticia
(nf-…) mandaron al paper. El enlace es la story del evento «strategy» y, desde que se abre, trade_id. Solo esas stories
(a-… y nf-…): la fase 2 manda al paper con la story de la noticia, que no es de ninguna apertura.
- Solo se añade: dos disparadores impiden DELETE y UPDATE (la tabla y su migración están en bot/store.py). Una fila
  igual a la última de la misma ventana×moneda×fase (decisión, motivo y operación) no se repite: un reinicio que vuelve
  a medir la misma apertura no duplica nada. Unas 100 filas al día (5 aperturas × 4 monedas × ≤ 5 fases).
- Cada fila sale al momento por el WebSocket ({type: "decision_apertura", ts, row}; no va en el estado inicial) y se
  guarda en un hilo con su propia conexión: escribir no para el bucle (las ids se reparten en memoria). Si guardar
  falla, queda en el registro (bot.log) y en memoria (la consulta la da) y se reintenta con espera creciente (fila a
  fila si el lote falla por una sola; una id que ya está en la base, p. ej. de otro bot a la vez, se cambia por una
  libre); tras unas 4 h, o si falla al cerrar, se da por perdida en la base. El motor sigue: add() nunca lanza.
- El motor lee al arrancar la última fila de cada apertura×moneda (ultima): lo ya decidido no se vuelve a decidir y
  una entrada ya hecha nunca se repite.
- GET /api/decisiones llama a consulta() en un hilo: por defecto los 3 últimos días, como mucho 2.000 filas, la más
  reciente primero."""
import json
import logging
import math
import queue
import re
import sqlite3
import threading
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import paper
from .models import now_ms
from .store import DECISIONES_COLS, migrate_decisiones

log = logging.getLogger("decisiones")
MAD = ZoneInfo("Europe/Madrid")
DAY = 86_400_000

FASES = ("medida", "entrada", "riesgo", "paper", "cierre", "perdida")
DECISIONES = ("entra", "no_entra", "rechazada", "perdida", "abierta", "cerrada", "esperando")
MOTIVOS = ("entra", "impulso_nulo", "impulso_bajo", "tarde", "sin_velas", "veto_noticia", "tope_dia", "riesgo_dia",
           "perdida_dia", "exposicion", "moneda_ocupada", "max_posiciones", "reglas", "aprobacion", "bot_parado",
           "objetivo", "stop", "tiempo", "otro")
FINALES = ("no_entra", "rechazada", "perdida", "cerrada")   # tras una de estas, esa apertura×moneda no vuelve a intentar
COLS = tuple(c for c, _ in DECISIONES_COLS)
DIAS_API = 3                  # GET /api/decisiones sin «desde»: los 3 últimos días
MAX_FILAS = 2000
CARGAR_DIAS = 9               # en memoria al arrancar: las aperturas de hoy y las operaciones aún abiertas (≤ 7 días)
BATCH_WAIT_S = 0.05
ERR_LOG_S = 300
RETRY_S = 1.0                 # lo que no se pudo guardar se reintenta a 1 s, luego el doble cada vez…
RETRY_MAX_S = 300.0           # …como mucho cada 5 min
RETRY_TRIES = 50              # unas 4 h reintentando: luego se da por perdida en la base (queda en bot.log)
ORDENES = ("a-", "nf-")       # stories de las órdenes del motor y de la entrada por noticia (las de la fase 2 no)
_RONDA = object()             # el hilo escritor despierta sin nada nuevo: toca reintentar
SRC_NOTICIA = "Entrada por noticia fiable"   # entrada_noticias.SRC (sin importarla: arrastra el aprendizaje)
OBSERVA = frozenset({"strategy", "riskko", "humanko", "blocked", "filled", "closed", "entrada_noticia"})
STORY_A = re.compile(r"^a-(?P<win>.+)-(?P<dia>\d{4}-\d{2}-\d{2})-(?P<coin>[a-z]+)-\d+$")
STORY_NF = re.compile(r"^nf-(?P<sid>.+)-(?P<coin>[a-z]+)$")


def _n(x, d: int = 2) -> str:
    return paper._n(x, d)


def _sg(x, d: int = 2) -> str:
    return paper._sg(x, d)


def _cut(s, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def limpio(x, depth: int = 0):
    """Copia apta para JSON estricto (la web no lee NaN ni Infinity): números no finitos a null, claves en texto."""
    if depth > 6:
        return None
    if isinstance(x, bool) or x is None or isinstance(x, (str, int)):
        return x
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {str(k): limpio(v, depth + 1) for k, v in x.items()}
    if isinstance(x, (list, tuple, set)):
        return [limpio(v, depth + 1) for v in x]
    return str(x)


def dia_madrid(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, MAD).date().isoformat()


def codigo_cierre(why: str | None) -> str:
    """objetivo | stop | tiempo | otro, del motivo de cierre del paper (paper._manage, paper.close)."""
    t = str(why or "")
    if t.startswith("Objetivo"):
        return "objetivo"
    if t.startswith(tuple(paper.STOP_NAMES.values())):
        return "stop"
    if t.startswith("Cierre por tiempo"):
        return "tiempo"
    return "otro"


def _fila_db(r) -> dict:
    row = dict(zip(COLS, r))
    try:
        row["datos"] = json.loads(row["datos"]) if row.get("datos") else None
    except (TypeError, ValueError):
        row["datos"] = None
    return row


class Decisiones:
    """Apunta y sirve las decisiones de las aperturas. bus.publish saca cada fila por el WebSocket; store es el de
    bot.db (store.db para leer al arrancar; las filas nuevas las guarda un hilo con su conexión)."""

    def __init__(self, bus, store, clock=now_ms, cargar_dias: int = CARGAR_DIAS):
        self.bus, self.store, self.clock = bus, store, clock
        self.cargar_dias = cargar_dias
        self.last: dict[tuple, dict] = {}        # (ventana_id, moneda) → última fila
        self.last_fase: dict[tuple, dict] = {}   # (ventana_id, moneda, fase) → última fila de esa fase
        self.by_story: dict[str, dict] = {}      # story → última fila con esa story (contexto del riesgo y del paper)
        self.unsaved: dict[int, dict] = {}       # id → fila aún sin guardar (consulta() las añade)
        self.n = self.errores = 0
        self.lento_ms = 0.0                      # la escritura más lenta vista en el bucle (add), en ms
        self.reintento_s = RETRY_S               # primera espera para reintentar lo que no se pudo guardar
        self.perdidas = 0                        # filas que no se pudieron guardar ni reintentando
        self._lock = threading.Lock()
        self._q: queue.Queue = queue.Queue()
        self._writer: threading.Thread | None = None
        self._wdb: sqlite3.Connection | None = None   # la conexión del hilo escritor
        self._err_at: dict[str, float] = {}
        self._memory = getattr(store, "_memory", False)
        db = store.db
        migrate_decisiones(db)   # Store ya la hace; aquí por si llega un store de antes
        top = db.execute("SELECT MAX(id) FROM decisiones_apertura").fetchone()[0] or 0
        try:
            seq = db.execute("SELECT seq FROM sqlite_sequence WHERE name='decisiones_apertura'").fetchone()
        except sqlite3.OperationalError:   # una versión vieja de la tabla sin AUTOINCREMENT: no hay sqlite_sequence
            seq = None
        self._next = max(int(top), int(seq[0]) if seq and seq[0] is not None else 0) + 1
        self._cargar(self.clock() - cargar_dias * DAY)
        if not self._memory:   # el hilo escritor arranca aquí, no en la primera decisión (en el bucle)
            self._start_writer()

    def _start_writer(self) -> None:
        self._writer = threading.Thread(target=self._write_loop, name="decisiones-writer", daemon=True)
        self._writer.start()

    # ---- registro con freno (un fallo repetido no llena bot.log)

    def _log_err(self, key: str, msg: str, *args) -> None:
        t = time.monotonic()
        self.errores += 1
        if t - self._err_at.get(key, float("-inf")) >= ERR_LOG_S:
            self._err_at[key] = t
            log.exception(msg, *args)

    # ---- memoria

    def _cargar(self, desde: int) -> None:
        try:
            rows = self.store.db.execute(f"SELECT {', '.join(COLS)} FROM decisiones_apertura WHERE ts >= ? "
                                         "ORDER BY id", (desde,)).fetchall()
        except sqlite3.Error:
            self._log_err("cargar", "Decisiones: no pude leer las guardadas al arrancar (se empieza sin ellas)")
            return
        for r in rows:
            self._indexar(_fila_db(r))
        if rows:
            log.info("Decisiones: %d filas de los últimos %d días en memoria", len(rows), self.cargar_dias)

    def _indexar(self, row: dict) -> None:
        k = (row["ventana_id"], row["coin"])
        self.last[k] = row
        self.last_fase[(*k, row.get("fase"))] = row
        if str(row.get("story") or "").startswith(ORDENES):   # la story de una noticia no liga nada del paper
            self.by_story[row["story"]] = row

    def _trim(self, now: int) -> None:
        cut = now - self.cargar_dias * DAY
        for d in (self.last, self.last_fase, self.by_story):
            for k in [k for k, r in d.items() if r["ts"] < cut]:
                del d[k]

    def ultima(self, ventana_id: str, coin: str) -> dict | None:
        """La última decisión de esa apertura×moneda (de esta sesión o de antes de reiniciar), o None."""
        return self.last.get((ventana_id, coin))

    def ventana(self, ventana_id: str) -> dict[str, dict]:
        """{moneda: última fila} de una apertura."""
        return {c: r for (v, c), r in self.last.items() if v == ventana_id}

    def fases(self, ventana_id: str) -> set[str]:
        """Las fases con alguna fila en esa apertura (de cualquier moneda)."""
        return {f for (v, _c, f) in self.last_fase if v == ventana_id}

    def de_story(self, story: str) -> dict | None:
        return self.by_story.get(story)

    def hubo_actividad(self, a: int, b: int) -> bool | None:
        """¿Guardó el bot algún evento entre a y b? (con miles de feeds, en marcha hay eventos cada minuto). Distingue una
        apertura que pasó con el bot parado de una que siguió una versión anterior sin guardar su decisión. None si no
        se puede saber."""
        try:
            return self.store.db.execute("SELECT 1 FROM events WHERE ts >= ? AND ts < ? LIMIT 1",
                                         (int(a), int(b))).fetchone() is not None
        except Exception:
            self._log_err("actividad", "Decisiones: no pude mirar si el bot estaba en marcha")
            return None

    # ---- añadir

    def add(self, ventana_id: str, coin: str, fase: str, decision: str, motivo_codigo: str | None, motivo: str | None,
            ts: int | None = None, ventana: str | None = None, dia: str | None = None, apertura_ms: int | None = None,
            impulso_pct: float | None = None, lado: str | None = None, preset: str | None = None,
            estrategia: str | None = None, trade_id: str | None = None, story: str | None = None,
            datos: dict | None = None) -> dict | None:
        """Añade una decisión (y la publica). Devuelve la fila, o None si es igual a la última de esa
        ventana×moneda×fase o no se pudo apuntar. Nunca lanza."""
        t0 = time.perf_counter()
        try:
            if not ventana_id or not coin or fase not in FASES or decision not in DECISIONES:
                log.warning("Decisiones: fila no válida (%r, %r, %r, %r): no se apunta", ventana_id, coin, fase, decision)
                return None
            code = motivo_codigo if motivo_codigo in MOTIVOS else "otro"
            motivo = _cut(motivo, 600) or None
            k = (str(ventana_id), str(coin))
            prev = self.last_fase.get((*k, fase))
            if prev and (prev["decision"], prev["motivo_codigo"], prev["motivo"], prev.get("trade_id")) == \
                    (decision, code, motivo, trade_id):
                return None
            now = self.clock()
            ts = int(now if ts is None else ts)
            last = self.last.get(k)
            if last and ts < last["ts"]:   # nunca antes que la anterior de la misma apertura×moneda (relojes distintos)
                ts = last["ts"]
            with self._lock:
                rid = self._next
                self._next += 1
            row = {"id": rid, "ts": ts, "ventana_id": k[0], "ventana": ventana,
                   "dia": dia or dia_madrid(int(apertura_ms) if apertura_ms else ts),
                   "apertura_ms": int(apertura_ms) if apertura_ms else None, "coin": k[1], "fase": fase,
                   "decision": decision, "motivo_codigo": code, "motivo": motivo,
                   "impulso_pct": round(_f(impulso_pct), 4) if _f(impulso_pct) is not None else None,
                   "lado": lado if lado in ("largo", "corto") else None, "preset": preset, "estrategia": estrategia,
                   "trade_id": trade_id, "story": story, "datos": limpio(datos) if datos else None}
            self._indexar(row)
            self.n += 1
            if self.n % 200 == 0:
                self._trim(now)
            self._guardar(row)
            try:
                self.bus.publish({"type": "decision_apertura", "ts": ts, "row": dict(row)})
            except Exception:
                self._log_err("publicar", "Decisiones: no pude publicar la fila %s", rid)
            return row
        except Exception:
            self._log_err("add", "Decisiones: no pude apuntar la decisión de %s en %s", coin, ventana_id)
            return None
        finally:
            self.lento_ms = max(self.lento_ms, (time.perf_counter() - t0) * 1000)

    def _valores(self, row: dict) -> tuple:
        return tuple(json.dumps(row["datos"], ensure_ascii=False, allow_nan=False) if c == "datos" and row["datos"]
                     is not None else row[c] for c in COLS)

    def _guardar(self, row: dict) -> None:
        if self._memory:   # base en memoria (pruebas): no admite otra conexión
            try:
                self.store.db.execute(f"INSERT INTO decisiones_apertura({', '.join(COLS)}) VALUES "
                                      f"({','.join('?' * len(COLS))})", self._valores(row))
                self.store.db.commit()
            except Exception:
                self._log_err("guardar", "Decisiones: no pude guardar la fila %s (queda en memoria)", row["id"])
            return
        with self._lock:
            self.unsaved[row["id"]] = row
        if self._writer is None:   # tras close(): se vuelve a arrancar
            self._start_writer()
        self._q.put(row)

    def _write_loop(self) -> None:
        """Hilo escritor: junta lo que llega seguido y lo guarda en un commit, con su propia conexión. Lo que no se pudo
        guardar sigue en unsaved (la consulta lo da) y se reintenta con espera creciente; al cerrar, una última vez."""
        retry: list[dict] = []   # {"row", "tries", "at"}: lo que falló, con cuándo toca el siguiente intento
        stop = False
        while not stop:
            wait = max(0.0, min(x["at"] for x in retry) - time.monotonic()) if retry else None
            try:
                first = self._q.get(timeout=wait)
            except queue.Empty:
                first = _RONDA
            batch = []
            if first is not _RONDA:
                if first is not None:
                    time.sleep(BATCH_WAIT_S)
                batch = [first]
                while first is not None:
                    try:
                        batch.append(self._q.get_nowait())
                    except queue.Empty:
                        break
            rows = [b for b in batch if b is not None]
            stop = len(rows) < len(batch)
            try:
                t = time.monotonic()
                due = [x for x in retry if stop or x["at"] <= t]
                if due:
                    ids = {id(x) for x in due}
                    retry = [x for x in retry if id(x) not in ids]
                retry += self._guardar_lote(rows, due, stop)
            except Exception:   # nunca se para el hilo: sin él no se guardaría nada más
                self._log_err("hilo", "Decisiones: fallo en el hilo escritor (sigue)")
            finally:
                for _ in batch:
                    self._q.task_done()
        if self._wdb is not None:
            try:
                self._wdb.close()
            except sqlite3.Error:
                pass
            self._wdb = None

    def _guardar_lote(self, rows: list[dict], due: list[dict], stop: bool) -> list[dict]:
        """Guarda las filas nuevas y las que tocaba reintentar. Devuelve las que vuelven a la espera (ninguna al cerrar o
        tras RETRY_TRIES intentos: esas se dan por perdidas en la base y quedan en bot.log)."""
        tries = {id(x["row"]): x["tries"] for x in due}
        todo = rows + [x["row"] for x in due]
        if not todo:
            return []
        mal = self._insertar(todo)
        out, perdidas = [], []
        for r in todo:
            if id(r) not in mal:
                continue
            n = tries.get(id(r), 0) + 1
            if stop or n >= RETRY_TRIES:
                perdidas.append(r)
            else:
                out.append({"row": r, "tries": n,
                            "at": time.monotonic() + min(RETRY_MAX_S, self.reintento_s * 2 ** (n - 1))})
        fuera = {id(r) for r in perdidas}
        with self._lock:
            for r in todo:
                if id(r) not in mal or id(r) in fuera:
                    self.unsaved.pop(r["id"], None)
        if perdidas:
            self.perdidas += len(perdidas)
            log.error("Decisiones: %d filas sin guardar %s (ids %s): no estarán en la base", len(perdidas),
                      "al cerrar" if stop else f"tras {RETRY_TRIES} intentos", ", ".join(str(r["id"]) for r in perdidas[:10]))
        return out

    def _conexion(self) -> sqlite3.Connection:
        if self._wdb is None:
            self._wdb = sqlite3.connect(self.store.path, timeout=60)
            self._wdb.execute("PRAGMA synchronous=NORMAL")
        return self._wdb

    def _deshacer(self) -> None:
        try:
            if self._wdb is not None:
                self._wdb.rollback()
        except sqlite3.Error:
            pass

    def _insertar(self, rows: list[dict]) -> set[int]:
        """Un commit con todas; si el lote falla por una restricción (una fila), fila a fila. Una id que ya está en la
        base (otro bot escribiendo a la vez) se cambia por una libre. Devuelve id() de las filas que no se guardaron."""
        sql = f"INSERT INTO decisiones_apertura({', '.join(COLS)}) VALUES ({','.join('?' * len(COLS))})"
        try:
            db = self._conexion()
            db.executemany(sql, [self._valores(r) for r in rows])
            db.commit()
            return set()
        except sqlite3.OperationalError:   # base bloqueada, disco lleno…: todo el lote espera al siguiente intento
            self._log_err("guardar", "Decisiones: no pude guardar %d filas (se reintentan; quedan en memoria y en la "
                                     "consulta)", len(rows))
            self._deshacer()
            return {id(r) for r in rows}
        except Exception:   # una restricción o un valor raro: fila a fila, para que una sola no tire el lote
            self._deshacer()
        mal: set[int] = set()
        for i, r in enumerate(rows):
            try:
                db = self._conexion()
                try:
                    db.execute(sql, self._valores(r))
                except sqlite3.IntegrityError:
                    if db.execute("SELECT 1 FROM decisiones_apertura WHERE id = ?", (r["id"],)).fetchone() is None:
                        raise
                    self._nueva_id(db, r)
                    db.execute(sql, self._valores(r))
                db.commit()
            except sqlite3.OperationalError:   # bloqueada o sin disco: el resto, al siguiente intento (sin esperar más)
                self._log_err("guardar", "Decisiones: no pude guardar %d filas (se reintentan; quedan en memoria y en "
                                         "la consulta)", len(rows) - i)
                self._deshacer()
                mal.update(id(x) for x in rows[i:])
                break
            except Exception:
                self._log_err("guardar_fila", "Decisiones: no pude guardar la fila %s (se reintenta)", r.get("id"))
                self._deshacer()
                mal.add(id(r))
        return mal

    def _nueva_id(self, db: sqlite3.Connection, row: dict) -> None:
        top = db.execute("SELECT MAX(id) FROM decisiones_apertura").fetchone()[0] or 0
        with self._lock:
            old = row["id"]
            row["id"] = new = max(self._next, int(top) + 1)
            self._next = new + 1
            if old in self.unsaved:
                self.unsaved[new] = self.unsaved.pop(old)
        log.warning("Decisiones: la id %s ya estaba en la base (¿otro bot escribiendo a la vez?): la fila va con la %s",
                    old, new)

    def flush(self) -> None:
        """Espera a que el hilo escritor haya intentado guardar todo lo pendiente (lo que falló sigue en unsaved y se
        reintenta aparte)."""
        if self._writer is not None:
            self._q.join()

    def close(self) -> None:
        if self._writer is not None:
            self._q.put(None)
            self._writer.join(timeout=30)
            self._writer = None

    # ---- consulta (GET /api/decisiones; en un hilo)

    def consulta(self, desde: int | None = None, ventana_id: str | None = None, coin: str | None = None,
                 limite: int | None = None) -> dict:
        """{rows, n, desde}: filas desde «desde» (ms; por defecto, 3 días), de una apertura y moneda si se piden, la más
        reciente primero (ts y luego id), como mucho 2.000. datos va como objeto."""
        desde = self.clock() - DIAS_API * DAY if desde is None else int(desde)
        limite = max(1, min(MAX_FILAS, int(limite or MAX_FILAS)))
        where, args = ["ts >= ?"], [desde]
        if ventana_id:
            where.append("ventana_id = ?")
            args.append(ventana_id)
        if coin:
            where.append("coin = ?")
            args.append(coin)
        # Las que el hilo aún no ha guardado (unos ms) también salen. Se copian ANTES de leer la base: lo que el hilo
        # guarde entretanto ya está en la lectura (si se copiaran después, una fila guardada justo entre las dos cosas
        # no estaría en ninguna).
        with self._lock:
            extra = [dict(r) for r in self.unsaved.values()]
        with self.store.reader() as db:
            got = db.execute(f"SELECT {', '.join(COLS)} FROM decisiones_apertura WHERE {' AND '.join(where)} "
                             "ORDER BY ts DESC, id DESC LIMIT ?", (*args, limite)).fetchall()
        rows = [_fila_db(r) for r in got]
        ids = {r["id"] for r in rows}
        extra = [r for r in extra if r["id"] not in ids and r["ts"] >= desde
                 and (not ventana_id or r["ventana_id"] == ventana_id) and (not coin or r["coin"] == coin)]
        if extra:
            rows = sorted(rows + extra, key=lambda r: (-r["ts"], -r["id"]))[:limite]
        return {"rows": rows, "n": len(rows), "desde": desde}

    # ---- gancho del bus: riesgo, paper y entrada por noticia (síncrono; nunca lanza)

    def observe(self, msg: dict) -> None:
        try:
            if not isinstance(msg, dict) or msg.get("type") != "event" or msg.get("kind") not in OBSERVA:
                return
            story = msg.get("story")
            if not isinstance(story, str) or not story:
                return
            kind, p = msg["kind"], msg.get("patch") or {}
            if kind == "entrada_noticia":
                return self._noticia(msg, p.get("entrada_noticia") or {})
            if kind == "strategy":
                if story.startswith("nf-") and (p.get("src") == SRC_NOTICIA or p.get("noticia")):
                    self._noticia_envio(msg, p)
                return   # las del motor las apunta el propio motor (con la medida y el plan)
            ctx = self._contexto(story, msg)
            if ctx is None:
                return
            ts = msg.get("ts")
            if kind == "riskko":
                r = p.get("risk") or {}
                why = r.get("why") or msg.get("text") or "sin motivo"
                bad = [c.get("t") for c in r.get("checks") or [] if isinstance(c, dict) and not c.get("ok")]
                self._add(ctx, "riesgo", "rechazada", r.get("motivo_codigo") or paper.motivo_codigo(why),
                          f"Vetada por el control de riesgo del paper: {why}", ts, story,
                          {"controles_fallidos": bad[:6]})
            elif kind == "blocked":
                ex = p.get("exec") or {}
                why = ex.get("why") or msg.get("text") or "sin motivo"
                self._add(ctx, "paper", "rechazada", ex.get("motivo_codigo") or paper.motivo_codigo(why, "blocked"),
                          f"El paper no la ejecutó: {why}", ts, story)
            elif kind == "humanko":
                h = p.get("human") or {}
                by = h.get("by") or msg.get("text") or "sin respuesta"
                self._add(ctx, "paper", "rechazada", h.get("motivo_codigo") or paper.motivo_codigo(by, "humanko"),
                          f"No se aprobó: {by}", ts, story)
            elif kind == "filled":
                ex = p.get("exec") or {}
                tid = p.get("trade_id") or ctx.get("trade_id")
                inst = "perpetuo" if ex.get("instrument") == "perp" else "contado"
                self._add(ctx, "paper", "abierta", "entra",
                          f"Abierta en el paper ({inst}, dinero ficticio): {msg.get('title') or ''}", ts, story,
                          {"entrada": ex.get("px"), "stop": ex.get("sl"), "objetivo": ex.get("tp2"),
                           "nominal_usdc": ex.get("notional"), "instrumento": inst, "deslizamiento_pb": ex.get("slip"),
                           "riesgo_con_costes_usdc": ex.get("stopLossUsd")}, tid)
            elif kind == "closed":
                res = p.get("result") or {}
                why, R = res.get("why") or "cerrada", _f(res.get("R"))
                tid = p.get("trade_id") or ctx.get("trade_id")
                txt = f"{why}: {_sg(R)} R" if R is not None else why
                if _f(res.get("pnl")) is not None:
                    txt += f" ({_sg(res['pnl'])} USDC, dinero ficticio)"
                self._add(ctx, "cierre", "cerrada", codigo_cierre(why), txt, ts, story,
                          {"R": R, "resultado_usdc": _f(res.get("pnl")), "resultado_pct": _f(res.get("pnl_pct")),
                           "entrada": _f(res.get("entry")), "salida": _f(res.get("exit")),
                           "costes_usdc": _f(res.get("fees")), "motivo_paper": why}, tid)
        except Exception:
            self._log_err("observe", "Decisiones: no pude apuntar el evento %s de %s", msg.get("kind"), msg.get("story"))

    def _add(self, ctx: dict, fase: str, decision: str, codigo: str, motivo: str, ts, story: str,
             datos: dict | None = None, trade_id: str | None = None) -> dict | None:
        return self.add(ctx["ventana_id"], ctx["coin"], fase, decision, codigo, motivo, ts=ts,
                        ventana=ctx.get("ventana"), dia=ctx.get("dia"), apertura_ms=ctx.get("apertura_ms"),
                        impulso_pct=ctx.get("impulso_pct"), lado=ctx.get("lado"), preset=ctx.get("preset"),
                        estrategia=ctx.get("estrategia"), trade_id=trade_id or ctx.get("trade_id"), story=story,
                        datos=datos)

    def _contexto(self, story: str, msg: dict) -> dict | None:
        """Apertura, moneda y etiquetas de una story del paper: las de su última fila o, para una operación de antes de
        existir esta tabla, las que dice la propia story (a-<ventana>-<fecha>-<moneda>-<n> o nf-<noticia>-<moneda>).
        None si no es del motor ni de la entrada por noticia: la fase 2 no va aquí, aunque mande al paper con la story de
        una noticia que la entrada por noticia también apuntó (esas filas llevan la story de la noticia, no la de una
        orden)."""
        if not story.startswith(ORDENES):
            return None
        r = self.by_story.get(story)
        if r:
            return r
        m = STORY_A.match(story)
        if m:
            vid = f"{m['win']}-{m['dia']}"
            return {"ventana_id": vid, "ventana": m["win"], "dia": m["dia"], "coin": m["coin"].upper(),
                    "apertura_ms": self._apertura_de(m["win"], m["dia"]), "lado": self._lado(msg),
                    "estrategia": None, "preset": None}
        m = STORY_NF.match(story)
        if m:
            return {"ventana_id": f"noticia-{m['sid']}", "ventana": "noticia", "coin": m["coin"].upper(),
                    "lado": self._lado(msg), "estrategia": SRC_NOTICIA, "preset": None}
        return None

    @staticmethod
    def _lado(msg: dict) -> str | None:
        res = (msg.get("patch") or {}).get("result") or {}
        side = res.get("side")
        return {"long": "largo", "short": "corto"}.get(side)

    @staticmethod
    def _apertura_de(win: str, dia: str) -> int | None:
        try:
            from . import apertura   # perezoso: apertura es grande y aquí solo hace falta la agenda
            return next((o["open_ms"] for o in apertura.openings_on(date.fromisoformat(dia)) if o["id"] == win), None)
        except Exception:
            return None

    # ---- entrada por noticia fiable (eventos «entrada_noticia» y su «strategy»)

    def _noticia_ctx(self, sid: str, coin: str, d, ts) -> dict:
        return {"ventana_id": f"noticia-{sid}", "ventana": "noticia", "dia": dia_madrid(int(ts or self.clock())),
                "coin": coin, "lado": "largo" if d == 1 else "corto" if d == -1 else None,
                "estrategia": SRC_NOTICIA, "preset": None}

    def _noticia_envio(self, msg: dict, p: dict) -> None:
        """La entrada por noticia manda la orden al paper: se apunta antes que lo que diga el riesgo o el paper."""
        st, tags = p.get("strat") or {}, p.get("tags") or {}
        sid, coin = p.get("noticia") or tags.get("story"), st.get("coin")
        if not sid or not coin:
            return
        ctx = {**self._noticia_ctx(sid, coin, 1 if st.get("side") == "long" else -1, msg.get("ts")),
               "impulso_pct": tags.get("mov_pct")}
        self._add(ctx, "entrada", "entra", "entra", f"Confirmada por el precio: se envía al paper. {msg.get('text') or ''}",
                  msg.get("ts"), msg["story"],
                  {"plan": {"stop_pct": st.get("stopPct"), "objetivo_R": st.get("tp2R"),
                            "instrumento": "perpetuo" if st.get("instrument") == "perp" else "contado",
                            "riesgo_pct": st.get("risk")},
                   "grupo": tags.get("grupo_nombre"), "acierto": tags.get("acierto"),
                   "umbral_pct": tags.get("umbral_pct"), "mov_pct": tags.get("mov_pct"), "noticia": sid})

    def _noticia(self, msg: dict, en: dict) -> None:
        """esperando → medida/esperando; caducada y vetada → entrada/no_entra; confirmada → entrada/entra (si su
        «strategy» no la apuntó ya). Sin moneda (la noticia no llegó a ninguna) no hay fila."""
        coin, estado, sid = en.get("moneda"), en.get("estado"), msg["story"]
        if not coin or estado not in ("esperando", "confirmada", "caducada", "vetada"):
            return
        ctx = {**self._noticia_ctx(sid, coin, en.get("dir"), msg.get("ts")), "impulso_pct": en.get("mov_pct")}
        k = (ctx["ventana_id"], coin)
        enviada = self.last_fase.get((*k, "entrada"))
        enviada = enviada if enviada and enviada["decision"] == "entra" else None
        grupo = (en.get("grupo") or {}).get("nombre")
        datos = {k2: en.get(k2) for k2 in ("umbral_pct", "mov_pct", "min", "expira") if en.get(k2) is not None}
        if grupo:
            datos["grupo"] = grupo
        motivo, code = en.get("motivo") or "", en.get("motivo_codigo")
        if estado == "esperando":
            self._add(ctx, "medida", "esperando", "otro", motivo, msg.get("ts"), sid, datos)
        elif estado == "confirmada":
            if not enviada:
                self._add(ctx, "entrada", "entra", "entra", motivo, msg.get("ts"), en.get("paper_story") or sid, datos)
        elif estado == "caducada":
            self._add(ctx, "entrada", "no_entra", code if code in MOTIVOS else
                      "sin_velas" if en.get("mov_pct") is None else "impulso_bajo", motivo, msg.get("ts"), sid, datos)
        elif enviada:   # vetada tras enviarla: la rechazó el riesgo o el paper, que ya lo apuntaron
            last = self.last.get(k)
            if not (last and last["decision"] == "rechazada"):
                self._add(ctx, "riesgo", "rechazada", code if code in MOTIVOS else "otro", motivo, msg.get("ts"),
                          enviada.get("story") or sid, datos)
        else:
            self._add(ctx, "entrada", "no_entra", code if code in MOTIVOS else "otro", motivo, msg.get("ts"), sid,
                      datos)
