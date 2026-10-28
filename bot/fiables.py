"""Noticias fiables: los grupos de noticias que aciertan el sentido a 4 h ≥ 60 % de forma constante (contrato 2.15).

Lo pidió el usuario el 28-10-2026: de todas las noticias grabadas (que aciertan el sentido un 49 % ajustado al mercado),
quedarse solo con las de los medios, fuentes y tipos que aciertan al menos un 60 % día tras día, aunque lleguen tarde.
Este módulo SOLO MIDE con lo que ya grabó el aprendizaje (tabla news_outcomes); no opera. Lo usan el veto de la estrategia
«6 diarias» (apertura, news_mode «fiables») y la entrada por noticia confirmada (bot/entrada_noticias.py) con
fiable_para(), que solo devuelve los grupos que además pasan la PUERTA PARA OPERAR. Sin ellos, ninguno de los dos hace
nada.

CRITERIOS FIJADOS DE ANTEMANO (no se cambian mirando el resultado; CRITERIOS):
- Unidad: una historia×moneda (la fila del primer medio: rol «primera» si existe, si no la de menor orden), con dirección
  (dir ≠ 0) y acc240 medido (rendimiento a 4 h desde el primer cierre de 1 min tras el triaje: cuando el bot ya pudo
  usarla, así cuentan también las que llegan tarde). Una noticia que vale para las cuatro monedas son 4 unidades, casi
  copias: por eso cada grupo dice también sus noticias distintas (n_historias).
- Acierto = dir × (acc240 − deriva) > 0, con deriva = media de acc240 de TODAS las filas medidas de esa moneda y día UTC
  (en un día bajista cualquier noticia bajista «acierta» sin saber nada). También el bruto (sin restar la deriva) y el de
  1 h (acc60 menos su deriva).
- Grupos: medio (outlet), fuente (feed), tipo×sentido y medio×sentido. Un feed que es el único de su medio (y el medio
  solo tiene ese feed) es el mismo grupo que el medio: no se prueba dos veces (duplicados).
- «fiable»: n ≥ 30 historias×moneda; ≥ 3 días distintos con ≥ 5; acierto ≥ 60 %; cota inferior de Wilson al 90 %
  (z = 1,645) ≥ 50 %; y acierto ≥ 50 % en CADA uno de esos días (constancia: no vale acertar un día sí y otro no).
  Con 14 días o más de datos, la constancia se mira por semanas (lunes a domingo UTC, cada una con ≥ 5): por días, un
  grupo que acierta de verdad el 60 % casi nunca pasaría 30 días seguidos (cambio fijado el 28-10-2026 con 3 días de
  datos, antes de que pueda cambiar nada).
- Solo días UTC completos y anteriores al cálculo (un día cuenta cuando han pasado sus 4 h de medida y 2 h de margen
  para el triaje): sin mirar el futuro. Como mucho los VENTANA_D últimos días.
- Azar, dos cuentas (PERMS permutaciones cada una, semilla fija; valor p = (1 + veces que el azar llega a lo real) /
  (1 + PERMS)):
  · «por_azar» (la fijada de antemano): la dirección barajada dentro de cada moneda×día.
  · «azar_bloques» (la que vale para operar): las direcciones de historias ENTERAS barajadas dentro de cada hora. Conserva
    que las cuatro monedas de una noticia van juntas y que los resultados se agrupan en el tiempo; la de moneda×día rompe
    las dos cosas y sale demasiado estrecha (revisión del 28-10-2026).
- Estado de cada grupo: «fiable» (pasa todo), «en_prueba» (acierta ≥ 60 % pero aún no tiene la muestra: n < 30 o menos
  de 3 días con ≥ 5) y «no_fiable» (el resto).

PUERTA PARA OPERAR (PUERTA; añadida tras la revisión estadística del 28-10-2026, antes de tener más datos: hoy no pasa
ningún grupo). Un grupo «fiable» solo se usa para entrar o vetar (opera = True) si además:
1. el conjunto supera al azar que conserva la hora: p de azar_bloques < 0,05;
2. tiene ≥ 30 noticias distintas y la cota de Wilson contando bloques de 30 min (no historias×moneda) es ≥ 50 %;
3. lo que cobra una operación sin cobertura: acierto bruto ≥ 50 % y la horquilla del usuario (stop 1,5 %, objetivo
   4,5 %, 24 h; aproximada con lo máximo que subió y bajó en 24 h desde la captura, y el stop primero si tocó los dos)
   da > 0 R de media en ≥ 30 noticias ya medidas a 24 h;
4. hacia delante y en vivo: en los días completos posteriores a la primera vez que pasó el criterio con solo los días
   anteriores, con las noticias que el bot usaría (senal = 1: no archivada, importancia suficiente, de esa moneda o
   global, y confirmada por la cripto si es macro), ≥ 30 noticias distintas con acierto ajustado ≥ 60 % y bruto ≥ 50 %.
El acierto ajustado mide información, no lo que gana una horquilla: por eso la puerta exige también el bruto y la
horquilla. Cada grupo lleva no_opera con lo que le falta.

Cálculo al arrancar y cada hora, en un hilo con su propia conexión de lectura. La semilla es fija y solo cuentan los días
completos, así que el resultado cambia solo cuando cambian los datos de entrada (firma_datos: días y filas medidas);
mientras no cambien, no se repite ni se guarda otro registro igual. Cada cálculo nuevo se AÑADE a la tabla
noticias_fiables (ts, json; dos disparadores impiden borrar o modificar filas) y se publica el estado «noticias_fiables»
(GET /api/noticias_fiables y estado inicial del WebSocket). Al arrancar se carga el último guardado.

Interfaz para el resto del bot:
    from bot import fiables
    fiables.fiable_para(outlet, fuente, tipo, dir) -> dict del grupo fiable que opera (el de mayor cota de Wilson) o None
    fiables.hay_fiables() -> ¿hay algún grupo que opera?
    fiables.monedas_de(titular, triaje, clase, reflejo) -> monedas a las que se aplica una noticia en vivo
"""
import asyncio
import hashlib
import json
import logging
import math
import os
import time
from datetime import datetime, timezone

import numpy as np

from . import aprendizaje, config, reflejo
from .models import now_ms

log = logging.getLogger("fiables")

MIN, H, DAY = 60_000, 3_600_000, 86_400_000
ENABLED = os.environ.get("BOT_FIABLES", "1") != "0"
TICK_S = 3600
PERMS = max(50, int(os.environ.get("BOT_FIABLES_PERM", "500")))   # nunca menos de 50 (≥ 300 para el valor p)
VENTANA_D = max(3, int(os.environ.get("BOT_FIABLES_DIAS", "30")))
SEED = 20261028
VERSION = 2                           # 2: azar por bloques, noticias distintas, puerta para operar (28-10-2026)
Z90 = aprendizaje.Z90                 # 1,645
MARGEN_MS = 240 * MIN + 2 * H         # un día UTC está completo cuando han pasado sus 4 h de medida y 2 h de margen
TOP_RESTO = 60                        # grupos que no son fiables que salen en el mensaje (los de más acierto)
MIN_N_LISTA = 10                      # ... con al menos tantas historias×moneda
SEMANAS_DESDE = 14                    # con tantos días de datos o más, la constancia es por semanas
BLOQUE_AZAR_MIN = 60                  # azar_bloques: historias barajadas dentro de cada hora
BLOQUE_WILSON_MIN = 30                # cota de Wilson de la puerta: un caso por bloque de 30 min
EPS = 1e-12
TIPOS = ("medio", "fuente", "tipo_sentido", "medio_sentido")
ESTADOS = ("fiable", "en_prueba", "no_fiable")
CRITERIOS = {
    "n_min": 30, "dias_min": 3, "n_dia": 5, "acierto_min": 0.60, "wilson90_min": 0.50, "dia_min": 0.50, "z": Z90,
    "horizonte_min": 240, "ventana_dias": VENTANA_D, "permutaciones": PERMS, "semanas_desde_dias": SEMANAS_DESDE,
    "unidad": "historia×moneda (la fila del primer medio), con dirección y acc240 medido",
    "ajuste": "acierto = dir × (acc240 − media de acc240 de todas las filas medidas de esa moneda y día UTC) > 0",
    "azar": "dirección barajada dentro de cada moneda×día (por_azar); historias enteras barajadas dentro de cada hora "
            "(azar_bloques, la que vale para operar)",
    "texto": ("Fiable: ≥ 30 historias×moneda (una noticia que vale para las 4 monedas cuenta 4 veces), ≥ 3 días con ≥ 5, "
              "acierto ajustado al mercado ≥ 60 %, cota inferior de Wilson al 90 % ≥ 50 % y ≥ 50 % en cada uno de esos "
              "días (por semanas desde 14 días de datos). Solo días completos anteriores al cálculo. Fijados de "
              "antemano: no se cambian mirando el resultado."),
}
PUERTA = {
    "p_azar_max": 0.05, "historias_min": 30, "wilson_bloques_min": 0.50, "bloque_min": BLOQUE_WILSON_MIN,
    "bruto_min": 0.50, "horquilla": {"stop_pct": 1.5, "objetivo_pct": 4.5, "horas": 24},
    "horquilla_historias_min": 30, "adelante_historias_min": 30, "adelante_acierto_min": 0.60, "adelante_bruto_min": 0.50,
    "texto": ("Para operar (entrar o vetar), además de fiable: el conjunto supera al azar que conserva la hora (p < 0,05); "
              "≥ 30 noticias distintas y cota de Wilson por bloques de 30 min ≥ 50 %; acierto bruto ≥ 50 % y horquilla "
              "1,5 %/4,5 % a 24 h > 0 R en ≥ 30 noticias; y hacia delante, con las noticias que usaría en vivo, ≥ 30 "
              "noticias distintas con acierto ≥ 60 % y bruto ≥ 50 %. Fijada el 28-10-2026 tras la revisión, antes de "
              "tener más datos."),
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS noticias_fiables(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS noticias_fiables_ts ON noticias_fiables(ts);
CREATE TRIGGER IF NOT EXISTS noticias_fiables_sin_borrar BEFORE DELETE ON noticias_fiables
  BEGIN SELECT RAISE(ABORT, 'noticias_fiables: los cálculos no se borran'); END;
CREATE TRIGGER IF NOT EXISTS noticias_fiables_sin_cambios BEFORE UPDATE ON noticias_fiables
  BEGIN SELECT RAISE(ABORT, 'noticias_fiables: los cálculos no se modifican'); END;
"""
COLS = ("story", "coin", "outlet", "rol", "orden", "fuente", "nombre", "tipo", "dir", "t0", "acc60", "acc240", "senal",
        "max_sube", "max_baja", "r1440")

# Último cálculo en uso (lo pone Fiables; fiable_para lo lee sin base ni red)
_estado: dict = {"msg": None, "idx": {}}


# ---------------------------------------------------------------- utilidades

def _iso(dia: int) -> str:
    return datetime.fromtimestamp(dia * DAY / 1000, timezone.utc).date().isoformat()


def _num(x: float, d: int = 1) -> str:
    return f"{x:.{d}f}".replace(".", ",")


def _pc(x: float) -> str:
    return f"{_num(x * 100, 0)} %"


def _r(x, d: int = 4):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(float(x), d)


def wilson_bajo(k, n, z: float = Z90):
    """Cota inferior del intervalo de Wilson (al 90 % con z = 1,645). Escalares o arrays; con n = 0, 0."""
    k, n = np.asarray(k, float), np.asarray(n, float)
    m = np.maximum(n, 1.0)
    p = k / m
    den = 1 + z * z / m
    c = p + z * z / (2 * m)
    r = z * np.sqrt(np.maximum(p * (1 - p) / m + z * z / (4 * m * m), 0.0))
    return np.where(n > 0, (c - r) / den, 0.0)


def valor_p(azar, real: float) -> float:
    """(1 + veces que el azar llega a lo real) / (1 + permutaciones). Sin permutaciones o sin nada real, 1."""
    a = np.asarray(azar, float)
    if not len(a) or real <= 0:
        return 1.0
    return float((1 + int((a >= real - EPS).sum())) / (1 + len(a)))


def clave(tipo: str, nombre, sentido: int | None = None) -> str:
    """Clave de un grupo: medio:<medio>, fuente:<feed>, tipo_sentido:<tipo>:<±1>, medio_sentido:<medio>:<±1>."""
    return f"{tipo}:{nombre}" if sentido is None else f"{tipo}:{nombre}:{int(sentido)}"


def ultimo_dia_completo(now: int) -> int:
    """Último día UTC (número de día) completo en now: anterior a hoy y con sus 4 h de medida y el margen pasados."""
    return (now - MARGEN_MS) // DAY - 1


def asegurar_tabla(db) -> None:
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='noticias_fiables'").fetchone() is None:
        db.executescript(SCHEMA)


def horquilla_R(d: int, sube, baja, r1440, stop: float = 1.5, objetivo: float = 4.5) -> float | None:
    """R aproximado de la horquilla del usuario a 24 h desde la captura (sin costes), con lo máximo que subió y bajó en
    24 h (en %, positivos) y el rendimiento a 24 h: −1 si tocó el stop (también si tocó los dos: el peor caso), +R
    objetivo si tocó el objetivo y, si no, el cierre a 24 h. None sin las 24 h medidas."""
    if r1440 is None or sube is None or baja is None:
        return None
    fav, contra = (float(sube), float(baja)) if d > 0 else (float(baja), float(sube))
    if contra >= stop - EPS:
        return -1.0
    if fav >= objetivo - EPS:
        return objetivo / stop
    return max(-1.0, min(objetivo / stop, d * float(r1440) / stop))


# ---------------------------------------------------------------- datos (en un hilo, conexión propia)

def ventana(now: int, dias: int = VENTANA_D) -> tuple[int, int]:
    """(primer, último) día UTC del cálculo: los `dias` días naturales que acaban en el último completo."""
    hasta = ultimo_dia_completo(now)
    return hasta - dias + 1, hasta


def firma_datos(db, now: int, dias: int = VENTANA_D, perms: int = PERMS) -> str:
    """Huella de lo que entra en el cálculo (días, filas medidas y sus valores, criterios, puerta, versión y
    permutaciones): con la misma huella el resultado es el mismo (semilla fija), así que no se recalcula."""
    d0, d1 = ventana(now, dias)
    try:
        n, s240, s60, d, s1440, sn = db.execute(
            "SELECT COUNT(*), TOTAL(acc240), TOTAL(acc60), TOTAL(dir), TOTAL(r1440), TOTAL(senal) FROM news_outcomes "
            "WHERE t0 >= ? AND t0 < ? AND acc240 IS NOT NULL", (d0 * DAY, (d1 + 1) * DAY)).fetchone()
    except Exception as e:   # sin tabla del aprendizaje todavía
        if "no such table" not in str(e):
            raise
        n, s240, s60, d, s1440, sn = 0, 0.0, 0.0, 0, 0.0, 0
    key = [d0, d1, int(n), round(float(s240), 4), round(float(s60), 4), int(d), round(float(s1440), 4), int(sn),
           perms, SEED, VERSION, {k: v for k, v in CRITERIOS.items() if k not in ("texto",)},
           {k: v for k, v in PUERTA.items() if k not in ("texto",)}]
    return hashlib.sha1(json.dumps(key, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def leer_filas(db, now: int, dias: int = VENTANA_D) -> list[dict]:
    """Filas de news_outcomes con acc240 medido de los días completos de la ventana."""
    d0, d1 = ventana(now, dias)
    try:
        rows = db.execute(f"SELECT {', '.join(COLS)} FROM news_outcomes WHERE t0 >= ? AND t0 < ? AND acc240 IS NOT NULL",
                          (d0 * DAY, (d1 + 1) * DAY)).fetchall()
    except Exception as e:
        if "no such table" in str(e):
            return []
        raise
    return [dict(zip(COLS, r)) for r in rows]


def unidades(filas: list[dict]) -> list[dict]:
    """Una fila por historia×moneda con dirección: la del primer medio (rol «primera»; si no, la de menor orden)."""
    best: dict[tuple, tuple] = {}
    for r in filas:
        if not r.get("dir"):
            continue
        k = (r["story"], r["coin"])
        rank = (0 if r.get("rol") == "primera" else 1, r["orden"] if r.get("orden") is not None else 99,
                r.get("t0") or 0)
        if k not in best or rank < best[k][0]:
            best[k] = (rank, r)
    return [r for _, r in best.values()]


def derivas(filas: list[dict], campo: str) -> dict[tuple, float]:
    """Media de `campo` (acc240 o acc60) de todas las filas medidas por (moneda, día UTC)."""
    acc: dict[tuple, list] = {}
    for r in filas:
        v = r.get(campo)
        if v is None:
            continue
        a = acc.setdefault((r["coin"], int(r["t0"]) // DAY), [0.0, 0])
        a[0] += float(v)
        a[1] += 1
    return {k: s / n for k, (s, n) in acc.items()}


# ---------------------------------------------------------------- cálculo (puro)

def _periodos(dia_abs: np.ndarray, n_dias: int) -> tuple[np.ndarray, int]:
    """Período de la constancia de cada unidad: su día (índice) con menos de SEMANAS_DESDE días de datos y, desde ahí,
    su semana UTC de lunes a domingo (índice)."""
    if n_dias >= SEMANAS_DESDE:
        sem = (dia_abs + 3) // 7   # el día 0 (1-1-1970) fue jueves: +3 lleva el lunes al principio de la semana
    else:
        sem = dia_abs
    u, inv = np.unique(sem, return_inverse=True)
    return inv.astype(np.int64), len(u)


def _evaluar(gid: np.ndarray, G: int, hit: np.ndarray, dia: np.ndarray, D: int, c: dict,
             per: np.ndarray | None = None, P: int | None = None) -> dict:
    """Por grupo: n, aciertos, n y aciertos por día y si pasa el criterio. gid −1 = sin grupo. per/P: período de la
    constancia (por defecto, el día)."""
    if per is None:
        per, P = dia, D
    m = gid >= 0
    g, h, dd, pp = gid[m], hit[m].astype(float), dia[m], per[m]
    n = np.bincount(g, minlength=G).astype(float)
    k = np.bincount(g, weights=h, minlength=G)
    idx = g * D + dd
    nd = np.bincount(idx, minlength=G * D).reshape(G, D).astype(float)
    kd = np.bincount(idx, weights=h, minlength=G * D).reshape(G, D)
    ok_d = nd >= c["n_dia"]
    ip = g * P + pp
    npd = np.bincount(ip, minlength=G * P).reshape(G, P).astype(float)
    kpd = np.bincount(ip, weights=h, minlength=G * P).reshape(G, P)
    ok_p = npd >= c["n_dia"]
    with np.errstate(divide="ignore", invalid="ignore"):
        frac = np.where(ok_p, kpd / np.maximum(npd, 1.0), np.inf)
    constante = frac.min(axis=1) >= c["dia_min"] - EPS if P else np.ones(G, bool)
    ndias = ok_d.sum(axis=1)
    p = k / np.maximum(n, 1.0)
    lo = wilson_bajo(k, n, c["z"])
    pasa = ((n >= c["n_min"]) & (ndias >= c["dias_min"]) & (p >= c["acierto_min"] - EPS)
            & (lo >= c["wilson90_min"] - EPS) & constante)
    return {"n": n, "k": k, "nd": nd, "kd": kd, "ndias": ndias, "p": p, "lo": lo, "pasa": pasa}


class _Familias:
    """Ids de grupo de cada unidad en las cuatro familias (las de sentido dependen de la dirección). Un feed que es el
    único de su medio, y el medio solo tiene ese feed, es el mismo grupo que el medio: queda fuera (duplicados)."""

    def __init__(self, units: list[dict]):
        self.outlets = sorted({str(u["outlet"]) for u in units})
        self.fuentes = sorted({str(u["fuente"]) for u in units if u.get("fuente")})
        self.tipos = sorted({str(u["tipo"]) for u in units if u.get("tipo")})
        o_ix = {o: i for i, o in enumerate(self.outlets)}
        f_ix = {f: i for i, f in enumerate(self.fuentes)}
        t_ix = {t: i for i, t in enumerate(self.tipos)}
        self.o = np.array([o_ix[str(u["outlet"])] for u in units], dtype=np.int64)
        f = np.array([f_ix[str(u["fuente"])] if u.get("fuente") else -1 for u in units], dtype=np.int64)
        self.t = np.array([t_ix[str(u["tipo"])] if u.get("tipo") else -1 for u in units], dtype=np.int64)
        self.nombre_fuente: dict[str, str] = {}
        for u in units:
            if u.get("fuente") and u.get("nombre"):
                self.nombre_fuente.setdefault(str(u["fuente"]), str(u["nombre"]))
        # feeds iguales a su medio: las mismas unidades (no dependen de la dirección, así que valen igual en el azar)
        de_f: dict[int, set] = {}
        de_o: dict[int, set] = {}
        for oi, fi in zip(self.o.tolist(), f.tolist()):
            de_o.setdefault(oi, set()).add(fi)
            if fi >= 0:
                de_f.setdefault(fi, set()).add(oi)
        self.duplicados = {fi: self.outlets[next(iter(os_))] for fi, os_ in de_f.items()
                           if len(os_) == 1 and de_o.get(next(iter(os_))) == {fi}}
        self.f = np.where(np.isin(f, list(self.duplicados)), -1, f) if self.duplicados else f

    def gids(self, direc: np.ndarray) -> dict[str, tuple[np.ndarray, int]]:
        up = (direc > 0).astype(np.int64)
        return {"medio": (self.o, len(self.outlets)), "fuente": (self.f, len(self.fuentes)),
                "tipo_sentido": (np.where(self.t >= 0, self.t * 2 + up, -1), 2 * len(self.tipos)),
                "medio_sentido": (self.o * 2 + up, 2 * len(self.outlets))}

    def describir(self, tipo: str, g: int) -> tuple[str, str, int | None, dict]:
        """(clave, nombre visible, sentido, campos para fiable_para) del grupo g de la familia tipo."""
        if tipo == "medio":
            o = self.outlets[g]
            return clave("medio", o), o, None, {"outlet": o}
        if tipo == "fuente":
            f = self.fuentes[g]
            return clave("fuente", f), self.nombre_fuente.get(f) or f, None, {"fuente": f}
        s = 1 if g % 2 else -1
        txt = "alcista" if s > 0 else "bajista"
        if tipo == "tipo_sentido":
            t = self.tipos[g // 2]
            return clave("tipo_sentido", t, s), f"{t} · {txt}", s, {"tipo_noticia": t}
        o = self.outlets[g // 2]
        return clave("medio_sentido", o, s), f"{o} · {txt}", s, {"outlet": o}


def _barajar(direc: np.ndarray, bloque: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """La dirección barajada dentro de cada bloque."""
    orden0 = np.argsort(bloque, kind="stable")
    orden = np.lexsort((rng.random(len(direc)), bloque))
    out = np.empty_like(direc)
    out[orden0] = direc[orden]
    return out


def _distintos(gid: np.ndarray, G: int, otro: np.ndarray, M: int, peso: np.ndarray | None = None) -> np.ndarray:
    """Por grupo, cuántos valores distintos de `otro` (historias, bloques…) tienen sus unidades (con peso: solo las de
    peso ≠ 0)."""
    m = gid >= 0 if peso is None else (gid >= 0) & (peso != 0)
    if not m.any():
        return np.zeros(G)
    par = np.unique(gid[m] * M + otro[m])
    return np.bincount(par // M, minlength=G).astype(float)


def _por_bloques(gid: np.ndarray, G: int, blk: np.ndarray, NB: int, hit: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(casos, aciertos) contando un caso por bloque de tiempo: el acierto de un bloque es la parte de sus unidades del
    grupo que aciertan (cien noticias de la misma media hora no valen como cien pruebas)."""
    m = gid >= 0
    if not m.any():
        return np.zeros(G), np.zeros(G)
    par = gid[m] * NB + blk[m]
    u, inv = np.unique(par, return_inverse=True)
    frac = np.bincount(inv, weights=hit[m].astype(float)) / np.bincount(inv)
    gg = u // NB
    return np.bincount(gg, minlength=G).astype(float), np.bincount(gg, weights=frac, minlength=G)


def calcular(filas: list[dict], now: int, perms: int = PERMS, dias: int = VENTANA_D, firma: str | None = None) -> dict:
    """Mensaje «noticias_fiables» con las filas medidas de la ventana (leer_filas). Puro: sin base ni red."""
    c = {**CRITERIOS, "ventana_dias": dias, "permutaciones": perms}
    pu = dict(PUERTA)
    d0, d1 = ventana(now, dias)
    units = [u for u in unidades(filas) if d0 <= int(u["t0"]) // DAY <= d1]
    der240, der60 = derivas(filas, "acc240"), derivas(filas, "acc60")
    dias_datos = sorted({int(u["t0"]) // DAY for u in units})
    horas = {}
    for u in units:
        horas.setdefault(int(u["t0"]) // DAY, set()).add(int(u["t0"]) // H)
    base = {"type": "noticias_fiables", "ts": now, "version": VERSION,
            "desde": _iso(dias_datos[0]) if dias_datos else None,
            "hasta": _iso(dias_datos[-1]) if dias_datos else None, "dias": len(dias_datos),
            "dias_lista": [_iso(d) for d in dias_datos], "ventana": {"desde": _iso(d0), "hasta": _iso(d1)},
            "horas_por_dia": [{"dia": _iso(d), "horas": len(horas[d])} for d in dias_datos],
            "unidades": len(units), "historias": len({u["story"] for u in units}), "criterios": c, "puerta": pu,
            "permutaciones": perms, "firma_datos": firma}
    if not units:
        return {**base, "probados": 0, "con_muestra": 0, "pasan": 0, "operan": 0, "duplicados": 0, "por_azar": 0.0,
                "por_azar_p95": 0, "por_azar_max": 0, "p_azar": 1.0,
                "azar_bloques": {"media": 0.0, "p95": 0, "max": 0, "p": 1.0, "bloque_min": BLOQUE_AZAR_MIN},
                "familias": {t: {"probados": 0, "con_muestra": 0, "pasan": 0, "por_azar": 0.0, "por_azar_bloques": 0.0}
                             for t in TIPOS},
                "grupos": [], "nota": ("Sin noticias medidas en días completos: no hay grupos fiables. La entrada por "
                                       "noticia no entra y el veto de «6 diarias» no veta nada.")}
    D = len(dias_datos)
    d_ix = {d: i for i, d in enumerate(dias_datos)}
    dia_abs = np.array([int(u["t0"]) // DAY for u in units], dtype=np.int64)
    dia = np.array([d_ix[d] for d in dia_abs.tolist()], dtype=np.int64)
    per, P = _periodos(dia_abs, D)
    direc = np.array([1 if u["dir"] > 0 else -1 for u in units], dtype=np.int64)
    keys = [(u["coin"], int(u["t0"]) // DAY) for u in units]
    aj = np.array([float(u["acc240"]) - der240.get(k, 0.0) for u, k in zip(units, keys)])
    bruto = np.array([float(u["acc240"]) for u in units])
    aj60 = np.array([float(u["acc60"]) - der60.get(k, 0.0) if u.get("acc60") is not None else np.nan
                     for u, k in zip(units, keys)])
    v60 = np.isfinite(aj60)
    vivo = np.array([u.get("senal") == 1 for u in units], dtype=bool)
    hq = pu["horquilla"]
    hqR = np.array([np.nan if (x := horquilla_R(int(dd), u.get("max_sube"), u.get("max_baja"), u.get("r1440"),
                                                  hq["stop_pct"], hq["objetivo_pct"])) is None else x
                    for u, dd in zip(units, direc)])
    vhq = np.isfinite(hqR)
    # historias: índice, hora (la de su primera unidad) y sentido de cada una
    s_ix: dict = {}
    s_code = np.array([s_ix.setdefault(u["story"], len(s_ix)) for u in units], dtype=np.int64)
    S = len(s_ix)
    s_t0 = np.full(S, np.iinfo(np.int64).max, dtype=np.int64)
    np.minimum.at(s_t0, s_code, np.array([int(u["t0"]) for u in units], dtype=np.int64))
    s_sig = direc[np.unique(s_code, return_index=True)[1]]   # el sentido de su primera unidad
    s_blk_azar = np.unique(s_t0 // (BLOQUE_AZAR_MIN * MIN), return_inverse=True)[1].astype(np.int64)
    u_w, blk_w = np.unique(s_t0[s_code] // (BLOQUE_WILSON_MIN * MIN), return_inverse=True)
    blk_w, NBW = blk_w.astype(np.int64), len(u_w)
    fam = _Familias(units)
    hit = direc * aj > 0
    hit_b = direc * bruto > 0
    hit60 = (direc * np.nan_to_num(aj60) > 0) & v60
    gids = fam.gids(direc)
    real = {t: _evaluar(g, G, hit, dia, D, c, per, P) for t, (g, G) in gids.items()}
    pasan_real = int(sum(int(real[t]["pasa"].sum()) for t in TIPOS))

    # azar 1 (el fijado): la dirección barajada dentro de cada moneda×día
    b_ix = {k: i for i, k in enumerate(sorted(set(keys)))}
    bloque = np.array([b_ix[k] for k in keys], dtype=np.int64)
    rng = np.random.default_rng(SEED)
    azar = {t: [] for t in TIPOS}
    for _ in range(perms):
        dp = _barajar(direc, bloque, rng)
        hp = dp * aj > 0
        for t, (g, G) in fam.gids(dp).items():
            azar[t].append(int(_evaluar(g, G, hp, dia, D, c, per, P)["pasa"].sum()))
    tot_azar = np.sum([azar[t] for t in TIPOS], axis=0) if perms else np.zeros(1)
    # azar 2 (el de operar): el sentido de historias enteras barajado dentro de cada hora (conserva el tiempo y las
    # cuatro monedas de cada noticia juntas)
    rng_b = np.random.default_rng(SEED + 1)
    azar_b = {t: [] for t in TIPOS}
    for _ in range(perms):
        nuevo = _barajar(s_sig, s_blk_azar, rng_b)
        dp = direc * (nuevo * s_sig)[s_code]
        hp = dp * aj > 0
        for t, (g, G) in fam.gids(dp).items():
            azar_b[t].append(int(_evaluar(g, G, hp, dia, D, c, per, P)["pasa"].sum()))
    tot_b = np.sum([azar_b[t] for t in TIPOS], axis=0) if perms else np.zeros(1)
    p_azar, p_b = valor_p(tot_azar if perms else [], pasan_real), valor_p(tot_b if perms else [], pasan_real)

    # hacia delante: primer día en que cada grupo pasa el criterio con solo los días anteriores
    primero = {t: np.full(G, D, dtype=np.int64) for t, (g, G) in gids.items()}
    for j in range(c["dias_min"], D):
        antes = dia < j
        per_j, P_j = _periodos(dia_abs[antes], j) if antes.any() else (per[antes], 1)
        per_full = np.zeros(len(units), dtype=np.int64)
        per_full[antes] = per_j
        for t, (g, G) in gids.items():
            ev = _evaluar(np.where(antes, g, -1), G, hit, dia, D, c, per_full, P_j)
            nuevos = ev["pasa"] & (primero[t] == D)
            primero[t][nuevos] = j

    # base del sentido: lo que acertaban todas las noticias del mismo sentido en esa moneda y día
    celda = bloque * 2 + (direc > 0)
    base_c = np.bincount(celda, weights=hit.astype(float), minlength=2 * len(b_ix)) / np.maximum(
        np.bincount(celda, minlength=2 * len(b_ix)), 1)
    base_u = base_c[celda]
    grupos, familias = [], {}
    for t, (g_arr, G) in gids.items():
        ev = real[t]
        m = g_arr >= 0
        gm = g_arr[m]
        if G == 0:
            familias[t] = {"probados": 0, "con_muestra": 0, "pasan": 0, "por_azar": 0.0, "por_azar_bloques": 0.0}
            if t == "fuente":
                familias[t]["duplicados"] = len(fam.duplicados)
            continue
        cnt = lambda w: np.bincount(gm, weights=w[m].astype(float), minlength=G)
        kb, n60, k60 = cnt(hit_b), cnt(v60), cnt(hit60)
        nh = _distintos(g_arr, G, s_code, S)
        nb, kbq = _por_bloques(g_arr, G, blk_w, NBW, hit)
        lo_b = wilson_bajo(kbq, nb, c["z"])
        nhq = _distintos(g_arr, G, s_code, S, vhq.astype(float))
        sum_hq = cnt(np.nan_to_num(hqR))
        n_hq = cnt(vhq)
        # vivo (exploración): solo las noticias que el bot usaría en vivo (senal = 1)
        gv = np.where(vivo, g_arr, -1)
        mv = gv >= 0
        cv = lambda w: np.bincount(gv[mv], weights=w[mv].astype(float), minlength=G)
        nv, kv, kbv, nhv = cv(np.ones(len(units))), cv(hit), cv(hit_b), _distintos(gv, G, s_code, S)
        # hacia delante y en vivo: desde el primer día elegido
        ga = np.where(vivo & (m & (dia >= primero[t][np.maximum(g_arr, 0)])), g_arr, -1)
        ma = ga >= 0
        ca = lambda w: np.bincount(ga[ma], weights=w[ma].astype(float), minlength=G)
        na, ka, kba, nha = ca(np.ones(len(units))), ca(hit), ca(hit_b), _distintos(ga, G, s_code, S)
        bs = cnt(base_u)
        trip = np.unique((gm * D + dia[m]) * S + s_code[m])   # noticias distintas por grupo y día
        nhd = np.bincount(trip // S, minlength=G * D).reshape(G, D)
        probados = int((ev["n"] > 0).sum())
        con_muestra = int(((ev["n"] >= c["n_min"]) & (ev["ndias"] >= c["dias_min"])).sum())
        familias[t] = {"probados": probados, "con_muestra": con_muestra, "pasan": int(ev["pasa"].sum()),
                       "por_azar": round(float(np.mean(azar[t])), 2) if perms else 0.0,
                       "por_azar_bloques": round(float(np.mean(azar_b[t])), 2) if perms else 0.0}
        if t == "fuente":
            familias[t]["duplicados"] = len(fam.duplicados)
        for g in np.flatnonzero((ev["n"] >= MIN_N_LISTA) | ev["pasa"]):
            n = int(ev["n"][g])
            cl, nombre, sentido, campos = fam.describir(t, int(g))
            fiable = bool(ev["pasa"][g])
            muestra = n >= c["n_min"] and int(ev["ndias"][g]) >= c["dias_min"]
            estado = "fiable" if fiable else ("en_prueba" if not muestra and ev["p"][g] >= c["acierto_min"] - EPS
                                              else "no_fiable")
            por_dia = [{"dia": _iso(dias_datos[j]), "n": int(ev["nd"][g, j]), "n_historias": int(nhd[g, j]),
                        "acierto": _r(ev["kd"][g, j] / ev["nd"][g, j])} for j in range(D) if ev["nd"][g, j] > 0]
            ade = {"desde": _iso(dias_datos[primero[t][g]]) if primero[t][g] < D else None, "n": int(na[g]),
                   "n_historias": int(nha[g]), "acierto": _r(ka[g] / na[g]) if na[g] else None,
                   "acierto_bruto": _r(kba[g] / na[g]) if na[g] else None}
            gr = {"clave": cl, "tipo": t, "nombre": nombre, "sentido": sentido, "n": n, "n_historias": int(nh[g]),
                  "dias": len(por_dia), "dias_validos": int(ev["ndias"][g]), "acierto": _r(ev["p"][g]),
                  "acierto_bruto": _r(kb[g] / n) if n else None,
                  "acierto_1h": _r(k60[g] / n60[g]) if n60[g] else None, "n_1h": int(n60[g]),
                  "ic90_bajo": _r(ev["lo"][g]), "bloques": int(nb[g]), "ic90_bajo_bloques": _r(lo_b[g]),
                  "base_sentido": _r(bs[g] / n) if n else None,
                  "horquilla_R": _r(sum_hq[g] / n_hq[g], 3) if n_hq[g] else None, "n_horquilla": int(nhq[g]),
                  "vivo": {"n": int(nv[g]), "n_historias": int(nhv[g]), "acierto": _r(kv[g] / nv[g]) if nv[g] else None,
                           "acierto_bruto": _r(kbv[g] / nv[g]) if nv[g] else None},
                  "adelante": ade, "por_dia": por_dia, "estado": estado, **campos}
            gr["no_opera"] = _no_opera(gr, p_b, pu)
            gr["opera"] = not gr["no_opera"]
            grupos.append(gr)
    fiab = sorted((x for x in grupos if x["estado"] == "fiable"), key=lambda x: (-x["ic90_bajo"], -x["n"]))
    resto = sorted((x for x in grupos if x["estado"] != "fiable" and x["n"] >= MIN_N_LISTA),
                   key=lambda x: (-(x["acierto"] or 0), -x["n"], x["clave"]))[:TOP_RESTO]
    pasan = len(fiab)
    operan = sum(1 for x in fiab if x["opera"])
    por_azar = round(float(np.mean(tot_azar)), 2) if perms else 0.0
    p95 = int(np.percentile(tot_azar, 95)) if perms else 0
    mx = int(np.max(tot_azar)) if perms else 0
    azb = {"media": round(float(np.mean(tot_b)), 2) if perms else 0.0,
           "p95": int(np.percentile(tot_b, 95)) if perms else 0, "max": int(np.max(tot_b)) if perms else 0,
           "p": round(p_b, 4), "bloque_min": BLOQUE_AZAR_MIN}
    probados = sum(f["probados"] for f in familias.values())
    con_muestra = sum(f["con_muestra"] for f in familias.values())
    periodo = f"{D} día{'s' if D != 1 else ''} completo{'s' if D != 1 else ''} ({base['desde']} a {base['hasta']})"
    nota = (f"{pasan} de {con_muestra} grupos con la muestra mínima pasan los criterios fijados de antemano "
            f"({probados} probados) con {len(units)} historias×moneda ({base['historias']} noticias distintas) de "
            f"{periodo}. Por azar pasarían {_num(por_azar)} barajando la dirección dentro de cada moneda y día "
            f"(p = {_num(p_azar, 3)}) y {_num(azb['media'])} barajando historias enteras dentro de cada hora "
            f"(p = {_num(p_b, 3)}).")
    if not pasan:
        nota += (" Sin grupos fiables: la entrada por noticia no entra y el veto de «6 diarias» no veta nada. Con tan "
                 "pocos días, ninguna noticia se separa del azar de forma constante.")
    else:
        if p_b >= pu["p_azar_max"]:
            nota += " Con el azar que conserva la hora no se distinguen de la casualidad."
        else:
            nota += " Más que por azar, pero con pocos días: es una prueba en paper, no una ventaja demostrada."
        if operan:
            nota += (f" {operan} pasa{'n' if operan != 1 else ''} también la puerta para operar: la entrada por noticia "
                     "y el veto de «6 diarias» la usan en paper.")
        else:
            nota += (" Ninguno pasa la puerta para operar (azar que conserva la hora, noticias distintas, acierto bruto y "
                     "horquilla, y hacia delante con lo que usaría en vivo): la entrada por noticia no entra y el veto "
                     "de «6 diarias» no veta nada.")
    if fam.duplicados:
        nota += f" {len(fam.duplicados)} feed{'s' if len(fam.duplicados) != 1 else ''} igual a su medio no se prueba dos veces."
    return {**base, "probados": probados, "con_muestra": con_muestra, "pasan": pasan, "operan": operan,
            "duplicados": len(fam.duplicados), "por_azar": por_azar, "por_azar_p95": p95, "por_azar_max": mx,
            "p_azar": round(p_azar, 4), "azar_bloques": azb, "familias": familias, "grupos": fiab + resto, "nota": nota}


def _no_opera(g: dict, p_b: float, pu: dict) -> list[str]:
    """Lo que le falta a un grupo para pasar la puerta para operar (lista vacía: opera)."""
    out = []
    if g["estado"] != "fiable":
        return ["no pasa los criterios fijados"]
    if p_b >= pu["p_azar_max"]:
        out.append(f"el conjunto no supera al azar que conserva la hora (p = {_num(p_b, 3)})")
    if g["n_historias"] < pu["historias_min"]:
        out.append(f"{g['n_historias']} noticias distintas (hacen falta {pu['historias_min']})")
    if (g["ic90_bajo_bloques"] or 0) < pu["wilson_bloques_min"] - EPS:
        out.append(f"cota de Wilson por bloques de {pu['bloque_min']} min {_pc(g['ic90_bajo_bloques'] or 0)} "
                   f"({g['bloques']} bloques; hace falta {_pc(pu['wilson_bloques_min'])})")
    if (g["acierto_bruto"] or 0) < pu["bruto_min"] - EPS:
        out.append(f"acierto bruto {_pc(g['acierto_bruto'] or 0)}: sin cobertura, una operación perdería")
    hq = pu["horquilla"]
    txt = f"horquilla {_num(hq['stop_pct'])} %/{_num(hq['objetivo_pct'])} % a {hq['horas']} h"
    if g["n_horquilla"] < pu["horquilla_historias_min"]:
        out.append(f"{txt} medida en {g['n_horquilla']} noticias (hacen falta {pu['horquilla_historias_min']})")
    elif (g["horquilla_R"] or 0) <= 0:
        out.append(f"{txt}: {_num(g['horquilla_R'] or 0, 2)} R de media")
    a = g["adelante"]
    if a["desde"] is None:
        out.append("aún no hay días posteriores a su elección para medirlo hacia delante")
    elif a["n_historias"] < pu["adelante_historias_min"]:
        out.append(f"hacia delante y en vivo (desde el {a['desde']}): {a['n_historias']} noticias de "
                   f"{pu['adelante_historias_min']}")
    else:
        if (a["acierto"] or 0) < pu["adelante_acierto_min"] - EPS:
            out.append(f"hacia delante y en vivo acierta el {_pc(a['acierto'] or 0)}")
        if (a["acierto_bruto"] or 0) < pu["adelante_bruto_min"] - EPS:
            out.append(f"hacia delante y en vivo, bruto {_pc(a['acierto_bruto'] or 0)}")
    return out


# ---------------------------------------------------------------- lo que usan los demás módulos

def usar(msg: dict | None) -> None:
    """Pone en uso un cálculo (o ninguno, con None): lo que responde fiable_para. Solo los grupos fiables que pasan la
    puerta para operar (opera = True); un cálculo sin ese campo (de antes de la puerta) no pone ninguno."""
    idx = {}
    for g in (msg or {}).get("grupos") or []:
        if isinstance(g, dict) and g.get("estado") == "fiable" and g.get("opera") is True and g.get("clave"):
            idx[g["clave"]] = g
    _estado["msg"], _estado["idx"] = msg, idx


def ultimo() -> dict | None:
    return _estado["msg"]


def hay_fiables() -> bool:
    """¿Hay algún grupo fiable que pasa la puerta para operar?"""
    return bool(_estado["idx"])


def fiable_para(outlet: str | None, fuente: str | None, tipo: str | None, dir: int | None) -> dict | None:
    """El grupo FIABLE que opera al que pertenece una noticia (medio×sentido, medio, fuente o tipo×sentido) o None. Si
    cae en varios, el de mayor cota de Wilson. Sin dirección o sin grupos que operan, None."""
    idx = _estado["idx"]
    if not idx or not dir:
        return None
    s = 1 if dir > 0 else -1
    cands = []
    if outlet:
        cands += [clave("medio_sentido", outlet, s), clave("medio", outlet)]
    if fuente:
        cands.append(clave("fuente", fuente))
    if tipo:
        cands.append(clave("tipo_sentido", tipo, s))
    found = [idx[k] for k in cands if k in idx]
    return max(found, key=lambda g: (g.get("ic90_bajo") or 0, g.get("n") or 0)) if found else None


def monedas_de(titular: str | None, triaje: dict | None, clase: str | None, refl: dict | None) -> tuple[list[str], str]:
    """(monedas, motivo) a las que se aplica en vivo una noticia con dirección: las que nombra el triaje (si no es de
    cripto, solo cuando la cripto la confirmó: reflejo.cuenta) o las cuatro si es una macro que la cripto confirmó
    (reflejo.macro_confirmada). Lista vacía con el motivo; «esperando_reflejo» si aún falta el reflejo."""
    named = aprendizaje.triage_coins(triaje)
    necesita = reflejo.aplica(titular, triaje, clase)
    estado = (refl or {}).get("estado") if isinstance(refl, dict) else None
    if named:
        if not necesita or reflejo.cuenta(titular, triaje, refl, clase):
            return named, "nombra la moneda"
        if estado in (None, "pendiente"):
            return [], "esperando_reflejo"
        return [], f"no es de cripto y la cripto no la confirmó (reflejo {estado})"
    if clase == "macro" and necesita:
        if reflejo.macro_confirmada(titular, triaje, refl, clase):
            return list(config.COINS), "macro confirmada por la cripto: vale para las cuatro"
        if estado in (None, "pendiente"):
            return [], "esperando_reflejo"
        return [], f"macro que la cripto no confirmó (reflejo {estado})"
    return [], "no nombra ninguna moneda ni es una macro confirmada"


# ---------------------------------------------------------------- módulo

class Fiables:
    """Calcula los grupos fiables al arrancar y cada hora (en un hilo), guarda cada cálculo nuevo en noticias_fiables,
    lo publica («noticias_fiables») y lo pone en uso para fiable_para."""

    def __init__(self, bus, store, clock=now_ms, perms: int = PERMS, dias: int = VENTANA_D, tick_s: float = TICK_S):
        self.bus, self.store, self.clock = bus, store, clock
        self.perms, self.dias, self.tick_s = max(1, int(perms)), int(dias), tick_s
        self.err: str | None = None
        self.ultimo_s: float | None = None   # segundos del último cálculo
        asegurar_tabla(store.db)
        self.msg = self.cargar()

    def cargar(self) -> dict | None:
        """El último cálculo guardado (al arrancar): en uso y publicado al momento, antes de recalcular."""
        try:
            row = self.store.db.execute("SELECT json FROM noticias_fiables ORDER BY id DESC LIMIT 1").fetchone()
            msg = json.loads(row[0]) if row else None
        except Exception:
            log.exception("Noticias fiables: no pude leer el último cálculo guardado")
            msg = None
        if isinstance(msg, dict):
            usar(msg)
            self.bus.publish(msg)
            return msg
        return None

    def _calcular_hilo(self, now: int, previa: str | None) -> dict | None:
        with self.store.reader() as db:
            firma = firma_datos(db, now, self.dias, self.perms)
            if previa is not None and firma == previa:
                return None   # mismos datos: mismo resultado
            filas = leer_filas(db, now, self.dias)
        return calcular(filas, now, self.perms, self.dias, firma)

    async def step(self, now: int | None = None) -> dict | None:
        """Recalcula si cambiaron los datos; guarda, publica y pone en uso el resultado nuevo. Devuelve el mensaje en
        uso (el nuevo o el de antes)."""
        now = self.clock() if now is None else int(now)
        t = time.perf_counter()
        try:
            msg = await asyncio.to_thread(self._calcular_hilo, now, (self.msg or {}).get("firma_datos"))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.err = str(e)[:200] or type(e).__name__
            log.exception("Noticias fiables: fallo en el cálculo; se reintenta en la próxima ronda")
            return self.msg
        self.err = None
        if msg is None:
            return self.msg
        self.ultimo_s = round(time.perf_counter() - t, 3)
        try:
            self.store.db.execute("INSERT INTO noticias_fiables(ts, json) VALUES (?, ?)",
                                  (msg["ts"], json.dumps(msg, ensure_ascii=False)))
            self.store.db.commit()
        except Exception:
            log.exception("Noticias fiables: no pude guardar el cálculo (se usa igual)")
        self.msg = msg
        usar(msg)
        self.bus.publish(msg)
        log.info("Noticias fiables: %d de %d grupos pasan, %d operan (azar %.1f / por horas %.1f, p %.3f) con %d "
                 "historias×moneda de %d días (%.1f s)", msg["pasan"], msg["probados"], msg["operan"], msg["por_azar"],
                 msg["azar_bloques"]["media"], msg["azar_bloques"]["p"], msg["unidades"], msg["dias"], self.ultimo_s)
        return msg

    async def run(self) -> None:
        while True:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Noticias fiables: fallo en la ronda; sigue en la próxima")
            await asyncio.sleep(self.tick_s)

    def view(self) -> dict | None:
        """Último cálculo (para GET /api/noticias_fiables) o None si aún no hay ninguno."""
        return self.msg
