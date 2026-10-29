"""lab_vivo.db (docs/07, apartado 12): todas las pruebas del laboratorio vivo. Solo escribe el corredor; las tareas
leen en modo «ro». El servidor del bot no la abre (lee los JSON). Está en DATA_DIR\\lab_vivo, no en bot.db.

pruebas, series y eventos son de SOLO AÑADIR: dos disparadores impiden borrarlas o cambiarlas (como evolution_log).
No hay forma de «limpiar» el número de pruebas."""
import json
import sqlite3
import time
import zlib
from pathlib import Path

import numpy as np

from . import ajustes

ESQUEMA = """
CREATE TABLE IF NOT EXISTS hipotesis(id TEXT PRIMARY KEY, recibida INTEGER NOT NULL, autor TEXT, familia TEXT NOT NULL,
  familia_version INTEGER, titulo TEXT, firma TEXT NOT NULL, estado TEXT NOT NULL, prioridad INTEGER, exploratoria INTEGER,
  fuera_de_reglas INTEGER, cuerpo TEXT NOT NULL, admision TEXT, actualizada INTEGER);
CREATE TABLE IF NOT EXISTS variantes(id TEXT PRIMARY KEY, hipotesis TEXT NOT NULL, vid TEXT NOT NULL,
  firma TEXT NOT NULL UNIQUE, params TEXT NOT NULL, salida TEXT NOT NULL, cumple_reglas INTEGER NOT NULL,
  clase TEXT NOT NULL, etapa TEXT, motivos TEXT, frecuencia TEXT, avance_desde INTEGER, actualizada INTEGER);
CREATE TABLE IF NOT EXISTS pruebas(id INTEGER PRIMARY KEY AUTOINCREMENT, variante TEXT NOT NULL, etapa TEXT NOT NULL,
  ts INTEGER NOT NULL, epoca TEXT, firma_datos TEXT, firma_codigo TEXT, criterios INTEGER, perfil TEXT,
  cuenta_en_n INTEGER NOT NULL, n_dentro INTEGER, n_fuera INTEGER, R_dentro REAL, R_fuera REAL, p_fuera REAL,
  sr_fuera REAL, dias_fuera INTEGER, asimetria REAL, curtosis REAL, pasa INTEGER, motivos TEXT, metricas TEXT);
CREATE TABLE IF NOT EXISTS series(prueba INTEGER PRIMARY KEY, desde_dia TEXT NOT NULL, dias INTEGER NOT NULL,
  r_dia BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS operaciones(variante TEXT NOT NULL, tramo TEXT NOT NULL, k INTEGER NOT NULL,
  cuerpo TEXT NOT NULL, PRIMARY KEY(variante, tramo, k));
CREATE TABLE IF NOT EXISTS avance(id TEXT PRIMARY KEY, variante TEXT NOT NULL, moneda TEXT, lado TEXT,
  t_senal INTEGER, t_entrada INTEGER, t_salida INTEGER, R REAL, R_f REAL, pct REAL, motivo TEXT,
  completa INTEGER, previa INTEGER, cuerpo TEXT);
CREATE TABLE IF NOT EXISTS trabajos(id TEXT PRIMARY KEY, tipo TEXT NOT NULL, objetivo TEXT, prioridad INTEGER NOT NULL,
  estado TEXT NOT NULL, intentos INTEGER NOT NULL DEFAULT 0, creado INTEGER, empezado INTEGER, terminado INTEGER,
  pid INTEGER, punto_control TEXT, segundos REAL, error TEXT);
CREATE TABLE IF NOT EXISTS consolidaciones(ts INTEGER PRIMARY KEY, n_total INTEGER, n_fuera INTEGER, n_efectivo REAL,
  n_previo INTEGER, n_dsr REAL, bh_m INTEGER, cuerpo TEXT);
CREATE TABLE IF NOT EXISTS pruebas_previas(fuente TEXT PRIMARY KEY, n INTEGER NOT NULL, n_efectivo REAL,
  mismo_fuera INTEGER, p_conocidos INTEGER, archivo TEXT, nota TEXT);
CREATE TABLE IF NOT EXISTS previas_p(fuente TEXT NOT NULL, k INTEGER NOT NULL, p REAL, PRIMARY KEY(fuente, k));
CREATE TABLE IF NOT EXISTS familias(nombre TEXT NOT NULL, version INTEGER NOT NULL, sha TEXT NOT NULL, alta INTEGER,
  PRIMARY KEY(nombre, version));
CREATE TABLE IF NOT EXISTS noticias_vivas(id TEXT PRIMARY KEY, t INTEGER NOT NULL, cuerpo TEXT);
CREATE TABLE IF NOT EXISTS eventos(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, tipo TEXT NOT NULL,
  sujeto TEXT, texto TEXT NOT NULL, cuerpo TEXT);
CREATE TABLE IF NOT EXISTS estado_kv(clave TEXT PRIMARY KEY, valor TEXT);
CREATE INDEX IF NOT EXISTS pruebas_variante ON pruebas(variante, etapa);
CREATE INDEX IF NOT EXISTS variantes_hipotesis ON variantes(hipotesis);
CREATE INDEX IF NOT EXISTS trabajos_estado ON trabajos(estado, prioridad);
CREATE INDEX IF NOT EXISTS avance_variante ON avance(variante, t_entrada);
CREATE TRIGGER IF NOT EXISTS pruebas_sin_borrar BEFORE DELETE ON pruebas
  BEGIN SELECT RAISE(ABORT, 'pruebas: no se borran: son el número de pruebas acumulado'); END;
CREATE TRIGGER IF NOT EXISTS pruebas_sin_cambios BEFORE UPDATE ON pruebas
  BEGIN SELECT RAISE(ABORT, 'pruebas: no se modifican'); END;
CREATE TRIGGER IF NOT EXISTS series_sin_borrar BEFORE DELETE ON series
  BEGIN SELECT RAISE(ABORT, 'series: no se borran: van con el número de pruebas acumulado'); END;
CREATE TRIGGER IF NOT EXISTS series_sin_cambios BEFORE UPDATE ON series
  BEGIN SELECT RAISE(ABORT, 'series: no se modifican'); END;
CREATE TRIGGER IF NOT EXISTS eventos_sin_borrar BEFORE DELETE ON eventos
  BEGIN SELECT RAISE(ABORT, 'eventos: no se borran: son el historial del laboratorio'); END;
CREATE TRIGGER IF NOT EXISTS eventos_sin_cambios BEFORE UPDATE ON eventos
  BEGIN SELECT RAISE(ABORT, 'eventos: no se modifican'); END;
"""

CLASES = ("pendiente", "duplicada", "rechazada", "fuera_de_reglas", "descartada_rapida", "descartada_completa",
          "descartada_robustez", "candidata", "candidata_hermana", "revocada", "en_avance", "descartada_avance", "lista",
          "propuesta", "aceptada", "descartada_usuario", "invalidada", "error")
ETAPAS = ("rapida", "completa", "robustez", "avance", "estres", "canario", "referencia")


def _jd(x):
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating,)):
        return float(x)
    if isinstance(x, np.ndarray):
        return x.tolist()
    if isinstance(x, (set, tuple)):
        return list(x)
    raise TypeError(type(x))


def dumps(x) -> str:
    return json.dumps(x, ensure_ascii=False, default=_jd, separators=(",", ":"))


def conectar(ruta: Path | str | None = None, solo_lectura: bool = False) -> sqlite3.Connection:
    """Conexión a lab_vivo.db en modo WAL con busy_timeout de 30 s. solo_lectura: «ro» (las tareas)."""
    ruta = Path(ruta or ajustes.DB)
    if solo_lectura:
        con = sqlite3.connect(f"file:{ruta.as_posix()}?mode=ro", uri=True, timeout=30, check_same_thread=False)
    else:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(ruta), timeout=30, check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.executescript(ESQUEMA)
    con.execute("PRAGMA busy_timeout=30000")
    con.row_factory = sqlite3.Row
    return con


def ahora() -> int:
    return int(time.time() * 1000)


def evento(con: sqlite3.Connection, tipo: str, texto: str, sujeto: str | None = None, cuerpo=None) -> None:
    con.execute("INSERT INTO eventos(ts, tipo, sujeto, texto, cuerpo) VALUES (?, ?, ?, ?, ?)",
                (ahora(), tipo, sujeto, texto, dumps(cuerpo) if cuerpo is not None else None))


def kv_get(con: sqlite3.Connection, clave: str, defecto=None):
    r = con.execute("SELECT valor FROM estado_kv WHERE clave = ?", (clave,)).fetchone()
    if r is None:
        return defecto
    try:
        return json.loads(r[0])
    except (TypeError, ValueError):
        return defecto


def kv_set(con: sqlite3.Connection, clave: str, valor) -> None:
    con.execute("INSERT INTO estado_kv(clave, valor) VALUES (?, ?) ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor",
                (clave, dumps(valor)))


def serie_a_blob(x: np.ndarray) -> bytes:
    return zlib.compress(np.asarray(x, np.float32).tobytes(), 6)


def blob_a_serie(b: bytes) -> np.ndarray:
    return np.frombuffer(zlib.decompress(b), np.float32).astype(np.float64)


def insertar_prueba(con: sqlite3.Connection, fila: dict, serie: tuple[str, np.ndarray] | None = None) -> int:
    """Una prueba (solo se añade). cuenta_en_n = 1 solo en la primera medida de una variante: si ya hay otra que cuenta,
    esta no suma (la decide aquí el corredor, no la tarea)."""
    cols = ("variante", "etapa", "ts", "epoca", "firma_datos", "firma_codigo", "criterios", "perfil", "cuenta_en_n",
            "n_dentro", "n_fuera", "R_dentro", "R_fuera", "p_fuera", "sr_fuera", "dias_fuera", "asimetria", "curtosis",
            "pasa", "motivos", "metricas")
    f = dict(fila)
    if f.get("cuenta_en_n"):
        ya = con.execute("SELECT 1 FROM pruebas WHERE variante = ? AND cuenta_en_n = 1 LIMIT 1",
                         (f["variante"],)).fetchone()
        f["cuenta_en_n"] = 0 if ya else 1
    f.setdefault("ts", ahora())
    f["cuenta_en_n"] = int(bool(f.get("cuenta_en_n")))
    for k in ("motivos", "metricas"):
        if f.get(k) is not None and not isinstance(f[k], str):
            f[k] = dumps(f[k])
    if f.get("pasa") is not None:
        f["pasa"] = int(bool(f["pasa"]))
    cur = con.execute(f"INSERT INTO pruebas({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                      tuple(f.get(k) for k in cols))
    pid = int(cur.lastrowid)
    if serie is not None and len(serie[1]):
        con.execute("INSERT INTO series(prueba, desde_dia, dias, r_dia) VALUES (?, ?, ?, ?)",
                    (pid, serie[0], int(len(serie[1])), serie_a_blob(serie[1])))
    return pid


def n_total(con: sqlite3.Connection) -> int:
    """Variantes medidas alguna vez (una vez cada una), del laboratorio vivo."""
    return int(con.execute("SELECT COUNT(DISTINCT variante) FROM pruebas WHERE cuenta_en_n = 1").fetchone()[0])


def n_fuera(con: sqlite3.Connection) -> int:
    return int(con.execute("SELECT COUNT(DISTINCT variante) FROM pruebas WHERE etapa IN ('completa', 'robustez') "
                           "AND n_fuera IS NOT NULL").fetchone()[0])


def n_previo(con: sqlite3.Connection) -> int:
    return int(con.execute("SELECT COALESCE(SUM(n), 0) FROM pruebas_previas").fetchone()[0])


def actualizar_variante(con: sqlite3.Connection, vid: str, **campos) -> None:
    if not campos:
        return
    campos["actualizada"] = ahora()
    for k in ("motivos", "frecuencia"):
        if k in campos and campos[k] is not None and not isinstance(campos[k], str):
            campos[k] = dumps(campos[k])
    sets = ", ".join(f"{k} = ?" for k in campos)
    con.execute(f"UPDATE variantes SET {sets} WHERE id = ?", (*campos.values(), vid))


def actualizar_hipotesis(con: sqlite3.Connection, hid: str, **campos) -> None:
    if not campos:
        return
    campos["actualizada"] = ahora()
    if "admision" in campos and campos["admision"] is not None and not isinstance(campos["admision"], str):
        campos["admision"] = dumps(campos["admision"])
    sets = ", ".join(f"{k} = ?" for k in campos)
    con.execute(f"UPDATE hipotesis SET {sets} WHERE id = ?", (*campos.values(), hid))


def fila_dict(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in ("cuerpo", "admision", "params", "salida", "motivos", "frecuencia", "metricas", "punto_control"):
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d
