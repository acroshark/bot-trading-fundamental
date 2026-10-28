"""Almacenamiento en SQLite: eventos para el historial de la web y claves ya vistas para no repetir noticias."""
import asyncio
import json
import logging
import queue
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("store")
SEEN_BATCH = 2000         # claves vistas por commit del hilo escritor
BATCH_WAIT_S = 0.05       # el hilo escritor junta lo que llega seguido antes de hacer commit
IDLE_CHECKPOINT_S = 10.0  # sin claves nuevas, el hilo escritor vacía el WAL cada tanto
PRUNE_BATCH = 500         # filas por lote al podar: cada lote es corto y el bucle respira entre uno y otro

# Por qué entra o no en cada apertura (bot/decisiones.py, docs/04 §4): solo se añade, nada se borra ni se cambia.
DECISIONES_COLS = (("id", "INTEGER PRIMARY KEY AUTOINCREMENT"), ("ts", "INTEGER NOT NULL"),
                   ("ventana_id", "TEXT NOT NULL"), ("ventana", "TEXT"), ("dia", "TEXT"), ("apertura_ms", "INTEGER"),
                   ("coin", "TEXT NOT NULL"), ("fase", "TEXT"), ("decision", "TEXT"), ("motivo_codigo", "TEXT"),
                   ("motivo", "TEXT"), ("impulso_pct", "REAL"), ("lado", "TEXT"), ("preset", "TEXT"),
                   ("estrategia", "TEXT"), ("trade_id", "TEXT"), ("story", "TEXT"), ("datos", "TEXT"))
DECISIONES_TABLE = f"CREATE TABLE IF NOT EXISTS decisiones_apertura({', '.join(f'{c} {t}' for c, t in DECISIONES_COLS)})"
DECISIONES_SCHEMA = """
CREATE INDEX IF NOT EXISTS decisiones_apertura_ventana ON decisiones_apertura(ventana_id, coin);
CREATE INDEX IF NOT EXISTS decisiones_apertura_ts ON decisiones_apertura(ts);
CREATE TRIGGER IF NOT EXISTS decisiones_apertura_sin_borrar BEFORE DELETE ON decisiones_apertura
  BEGIN SELECT RAISE(ABORT, 'decisiones_apertura: las decisiones no se borran'); END;
CREATE TRIGGER IF NOT EXISTS decisiones_apertura_sin_cambios BEFORE UPDATE ON decisiones_apertura
  BEGIN SELECT RAISE(ABORT, 'decisiones_apertura: las decisiones no se modifican'); END;
"""


def migrate_decisiones(db: sqlite3.Connection) -> None:
    """Tabla decisiones_apertura (migración aditiva): la crea si falta y, en una base con una versión anterior de la
    tabla, añade las columnas que le falten (sin tocar sus filas; las que en la nueva son NOT NULL, admitiendo NULL),
    y después los índices y los disparadores que impiden borrar y cambiar."""
    db.execute(DECISIONES_TABLE)
    have = {r[1] for r in db.execute("PRAGMA table_info(decisiones_apertura)")}
    for col, typ in DECISIONES_COLS:
        if col not in have and col != "id":
            db.execute(f"ALTER TABLE decisiones_apertura ADD COLUMN {col} {typ.replace(' NOT NULL', '')}")
            log.info("decisiones_apertura: columna nueva %s", col)
    db.commit()
    db.executescript(DECISIONES_SCHEMA)


async def in_batches(prune, *args) -> int:
    """Repite una poda por lotes cediendo el bucle entre lote y lote: en una base grande no lo bloquea."""
    total = 0
    while n := prune(*args):
        total += n
        await asyncio.sleep(0)
    return total


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")   # seguro con WAL y mucho más rápido con miles de feeds
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS events(
              id TEXT PRIMARY KEY, ts INTEGER NOT NULL, story TEXT, agent TEXT, kind TEXT, body TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
            CREATE INDEX IF NOT EXISTS events_story ON events(story);
            CREATE INDEX IF NOT EXISTS events_kind_ts ON events(kind, ts);
            CREATE TABLE IF NOT EXISTS seen(key TEXT PRIMARY KEY, ts INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS seen_ts ON seen(ts);
            """
        )
        migrate_decisiones(self.db)
        # Claves vistas: las guarda por lotes un hilo con su propia conexión (commit y checkpoint fuera del bucle).
        # Hasta que se guardan están en _pending, así que is_seen responde al momento.
        self._memory = str(path) == ":memory:"
        self._pending: set[str] = set()
        self._wq: queue.Queue = queue.Queue()
        self._writer: threading.Thread | None = None

    def add_event(self, ev: dict) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?)",
            (ev["id"], ev["ts"], ev.get("story"), ev.get("agent"), ev.get("kind"), json.dumps(ev, ensure_ascii=False)),
        )
        self.db.commit()

    def recent_events(self, n: int, exclude: tuple[str, ...] = (), only: tuple[str, ...] = ()) -> list[dict]:
        where, args = "", []
        if exclude:
            where, args = f"WHERE kind NOT IN ({','.join('?' * len(exclude))})", list(exclude)
        elif only:
            where, args = f"WHERE kind IN ({','.join('?' * len(only))})", list(only)
        rows = self.db.execute(f"SELECT body FROM events {where} ORDER BY ts DESC, rowid DESC LIMIT ?", (*args, n)).fetchall()
        return [json.loads(r[0]) for r in reversed(rows)]

    def prune(self, kinds: tuple[str, ...], before_ts: int, batch: int = PRUNE_BATCH) -> int:
        """Borra un lote de eventos viejos de esos tipos; hay que repetir hasta que devuelva 0 (ver __main__.maintenance)."""
        marks = ",".join("?" * len(kinds))
        cur = self.db.execute(f"DELETE FROM events WHERE rowid IN (SELECT rowid FROM events WHERE kind IN ({marks}) "
                              "AND ts < ? LIMIT ?)", (*kinds, before_ts, batch))
        self.db.commit()
        return cur.rowcount

    def prune_seen(self, before_ts: int, batch: int = PRUNE_BATCH) -> int:
        """Borra un lote de claves vistas anteriores a before_ts (con índice en ts); repetir hasta que devuelva 0."""
        cur = self.db.execute("DELETE FROM seen WHERE rowid IN (SELECT rowid FROM seen WHERE ts < ? LIMIT ?)",
                              (before_ts, batch))
        self.db.commit()
        return cur.rowcount

    def events_since(self, ts: int, kinds: tuple[str, ...]) -> list[dict]:
        marks = ",".join("?" * len(kinds))
        rows = self.db.execute(f"SELECT body FROM events WHERE ts >= ? AND kind IN ({marks}) ORDER BY ts", (ts, *kinds)).fetchall()
        return [json.loads(r[0]) for r in rows]

    @contextmanager
    def reader(self):
        """Conexión propia de solo lectura para consultas pesadas desde un hilo (la principal es del bucle)."""
        if self._memory:   # una base en memoria (pruebas) no admite otra conexión
            yield self.db
            return
        db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10, check_same_thread=False)
        try:
            yield db
        finally:
            db.close()

    def is_seen(self, key: str) -> bool:
        return key in self._pending or self.db.execute("SELECT 1 FROM seen WHERE key=?", (key,)).fetchone() is not None

    def mark_seen(self, key: str, ts: int) -> None:
        if self._memory:
            self.db.execute("INSERT OR IGNORE INTO seen VALUES (?,?)", (key, ts))
            self.db.commit()
            return
        if key in self._pending:
            return
        if self._writer is None:
            self._start_writer()
        self._pending.add(key)
        self._wq.put((key, ts))

    def flush(self) -> None:
        """Espera a que el hilo escritor haya guardado todas las claves vistas pendientes."""
        if self._writer is not None:
            self._wq.join()

    def _start_writer(self) -> None:
        # Desde aquí los checkpoints del WAL los hace el hilo escritor: el bucle ya no se para 100 ms en un commit.
        self.db.execute("PRAGMA wal_autocheckpoint=0")
        self._writer = threading.Thread(target=self._write_loop, name="store-writer", daemon=True)
        self._writer.start()

    def _write_loop(self) -> None:
        """Hilo escritor: guarda las claves vistas por lotes (un commit por lote) y hace los checkpoints del WAL."""
        db = sqlite3.connect(self.path, timeout=60)
        db.execute("PRAGMA synchronous=NORMAL")
        stop = False
        while not stop:
            try:
                first = self._wq.get(timeout=IDLE_CHECKPOINT_S)
            except queue.Empty:
                try:
                    db.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except sqlite3.Error as e:
                    log.warning("Checkpoint del WAL fallido: %s", e)
                continue
            if first is not None:   # junta en un solo commit lo que llega seguido y deja respirar al bucle (GIL)
                time.sleep(BATCH_WAIT_S)
            batch = [first]
            while first is not None and len(batch) < SEEN_BATCH:
                try:
                    batch.append(self._wq.get_nowait())
                except queue.Empty:
                    break
            rows = [b for b in batch if b is not None]
            stop = len(rows) < len(batch)
            if rows:
                try:
                    db.executemany("INSERT OR IGNORE INTO seen VALUES (?,?)", rows)
                    db.commit()
                    self._pending.difference_update([k for k, _ in rows])
                except sqlite3.Error:   # se quedan en memoria: en esta sesión no se repiten igualmente
                    log.exception("No se pudieron guardar %d claves vistas", len(rows))
                    try:
                        db.rollback()
                    except sqlite3.Error:
                        pass
            for _ in batch:
                self._wq.task_done()
        db.close()

    def close(self) -> None:
        if self._writer is not None:
            self._wq.put(None)
            self._writer.join(timeout=30)
        self.db.close()
