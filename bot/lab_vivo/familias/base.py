"""Contrato de las familias del laboratorio vivo (docs/07, 6.1).

Una familia es un archivo de Python en bot/lab_vivo/familias/<nombre>.py con una clase que hereda de Familia. A partir
del Mercado (velas, barras, funding, aperturas…) da las SEÑALES DE ENTRADA sin mirar al futuro. Las salidas no son
suyas: las pone el libro con la gestión verificada del paper y una salida del catálogo (salidas.py). Así ninguna
familia puede inventarse una salida que rompa las reglas del usuario.

Reglas de las señales:
- Una por flanco: la señal sale cuando la condición EMPIEZA a cumplirse, no en cada minuto que dura.
- t = instante de la decisión (el cierre de la última vela usada), múltiplo de 60.000 ms. La entrada es la primera
  vela de 1 min que abre en t o después, a mercado, con el deslizamiento.
- stop_pct: distancia del stop en % para un stop estructural (NaN = el de la salida de la variante). El libro lo sube a
  1,5 % y veta los que pasan del tope.
- Prohibido: la red y escribir archivos (la tarea bloquea socket.socket antes de importar las familias).

Una familia nueva solo puede importar este módulo, bot.lab_vivo.indicadores, bot.lab_vivo.filtros, numpy y la
biblioteca estándar. Su «version» sube con CUALQUIER cambio del archivo (la tabla «familias» guarda su sha)."""
import math
from dataclasses import dataclass

import numpy as np

COINS = ("BTC", "ETH", "SOL", "XRP")
MIN, H, DAY = 60_000, 3_600_000, 86_400_000
LADOS = ("ambos", "largos", "cortos")


@dataclass(frozen=True)
class Param:
    tipo: str                      # "int" | "float" | "str" | "bool" | "lista"
    defecto: object
    opciones: tuple | None = None  # valores permitidos (si no, min y max)
    min: float | None = None
    max: float | None = None
    filtro: bool = False           # cuenta para el tope de 3 filtros cuando no está en su valor neutro
    neutro: object = None          # el valor que lo apaga ("ninguno", "todos", 0…)
    texto: str = ""                # para describir la variante en español
    nulo: bool = False             # admite None

    def validar(self, v) -> tuple[object, str | None]:
        """(valor normalizado, motivo del error o None)."""
        if v is None:
            if self.nulo or self.defecto is None:
                return None, None
            return v, "no puede ser nulo"
        try:
            if self.tipo == "int":
                if isinstance(v, bool) or (isinstance(v, float) and not float(v).is_integer()):
                    raise ValueError
                v = int(v)
            elif self.tipo == "float":
                if isinstance(v, bool):
                    raise ValueError
                v = float(v)
                if not math.isfinite(v):
                    raise ValueError
            elif self.tipo == "bool":
                if not isinstance(v, bool):
                    raise ValueError
            elif self.tipo == "str":
                if not isinstance(v, str):
                    raise ValueError
                v = v.strip()
            elif self.tipo == "lista":
                if isinstance(v, str):
                    v = [x for x in v.replace(";", ",").replace("+", ",").split(",") if x.strip()]
                if not isinstance(v, (list, tuple)):
                    raise ValueError
                v = sorted({str(x).strip() for x in v})
        except (TypeError, ValueError):
            return v, f"tiene que ser de tipo {self.tipo}"
        if self.opciones is not None:
            if self.tipo == "lista":
                malos = [x for x in v if x not in self.opciones]
                if malos or not v:
                    return v, f"valores permitidos: {', '.join(map(str, self.opciones))}"
            elif self.tipo in ("int", "float"):
                if not any(abs(float(v) - float(o)) < 1e-9 for o in self.opciones):
                    return v, f"valores permitidos: {', '.join(_num(o) for o in self.opciones)}"
            elif v not in self.opciones:
                return v, f"valores permitidos: {', '.join(map(str, self.opciones))}"
        if self.tipo in ("int", "float"):
            if self.min is not None and v < self.min - 1e-12:
                return v, f"mínimo {_num(self.min)}"
            if self.max is not None and v > self.max + 1e-12:
                return v, f"máximo {_num(self.max)}"
        return v, None

    def activo(self, v) -> bool:
        if not self.filtro:
            return False
        if isinstance(v, float) or isinstance(self.neutro, float):
            try:
                return abs(float(v) - float(self.neutro)) > 1e-12
            except (TypeError, ValueError):
                return v != self.neutro
        return v != self.neutro


def _num(x) -> str:
    if isinstance(x, float):
        s = f"{x:.4f}".rstrip("0").rstrip(".")
        return s.replace(".", ",")
    return str(x)


class Senales:
    """Arrays de numpy de la misma longitud, ordenados por t.
    t (int64, ms UTC, múltiplo de 60.000: la decisión) · moneda (int8, índice en Mercado.monedas) · dir (int8: +1 largo
    al contado, −1 corto en perpetuo) · stop_pct (float64, NaN = el de la salida) · ventana (int8, índice en
    apertura.LAB_WINDOWS o −1) · clave (int32 libre, tipo de señal) · prioridad (float64: si en el mismo minuto no
    caben todas, entran antes las de mayor prioridad) · objetivo_r (float64, NaN = el de la salida; solo la salida
    «OBJ») · fin_ms (int64, 0 = sin tope propio: cierre por tiempo de la señal, si es antes que el de la salida)."""

    CAMPOS = (("t", np.int64), ("moneda", np.int8), ("dir", np.int8), ("stop_pct", np.float64),
              ("ventana", np.int8), ("clave", np.int32), ("prioridad", np.float64), ("objetivo_r", np.float64),
              ("fin_ms", np.int64))

    def __init__(self, **kw):
        n = len(kw.get("t", ()))
        defaults = {"stop_pct": np.nan, "ventana": -1, "clave": 0, "prioridad": 0.0, "objetivo_r": np.nan, "fin_ms": 0}
        for k, dt in self.CAMPOS:
            v = kw.get(k)
            if v is None:
                v = np.full(n, defaults.get(k, 0), dtype=dt)
            else:
                v = np.asarray(v, dtype=dt).reshape(-1)
                if len(v) != n:
                    v = np.broadcast_to(v, (n,)).astype(dt) if len(v) == 1 else v
            setattr(self, k, v)
        if n:
            o = np.lexsort((self.moneda, -self.prioridad, self.t))
            if not np.array_equal(o, np.arange(n)):
                for k, _ in self.CAMPOS:
                    setattr(self, k, getattr(self, k)[o])

    def __len__(self) -> int:
        return len(self.t)

    @staticmethod
    def vacia() -> "Senales":
        return Senales(t=np.zeros(0, np.int64), moneda=np.zeros(0, np.int8), dir=np.zeros(0, np.int8))

    @staticmethod
    def juntar(partes) -> "Senales":
        partes = [p for p in partes if p is not None and len(p)]
        if not partes:
            return Senales.vacia()
        return Senales(**{k: np.concatenate([getattr(p, k) for p in partes]) for k, _ in Senales.CAMPOS})

    def filtrar(self, mask) -> "Senales":
        mask = np.asarray(mask, bool)
        return Senales(**{k: getattr(self, k)[mask] for k, _ in self.CAMPOS})

    def entre(self, desde_ms: int | None, hasta_ms: int | None) -> "Senales":
        m = np.ones(len(self), bool)
        if desde_ms is not None:
            m &= self.t >= desde_ms
        if hasta_ms is not None:
            m &= self.t < hasta_ms
        return self.filtrar(m)

    def iguales(self, otra: "Senales") -> bool:
        if len(self) != len(otra):
            return False
        for k, _ in self.CAMPOS:
            a, b = getattr(self, k), getattr(otra, k)
            if a.dtype.kind == "f":
                if not np.allclose(a, b, rtol=1e-9, atol=1e-12, equal_nan=True):
                    return False
            elif not np.array_equal(a, b):
                return False
        return True

    def como_lista(self, monedas=COINS, limite: int | None = None) -> list[dict]:
        out = []
        for i in range(len(self) if limite is None else min(limite, len(self))):
            out.append({"t": int(self.t[i]), "moneda": monedas[int(self.moneda[i])], "dir": int(self.dir[i]),
                        "stop_pct": None if not np.isfinite(self.stop_pct[i]) else round(float(self.stop_pct[i]), 4),
                        "clave": int(self.clave[i])})
        return out


def emitir(t, moneda: int, d, stop_pct=None, clave=0, prioridad=None, ventana=-1, objetivo_r=None,
           fin_ms=None) -> Senales:
    """Señales de una moneda a partir de arrays (o escalares que se repiten)."""
    t = np.asarray(t, np.int64).reshape(-1)
    n = len(t)

    def rep(x, dt, dflt):
        if x is None:
            return np.full(n, dflt, dt)
        a = np.asarray(x, dt).reshape(-1)
        return np.full(n, a[0], dt) if len(a) == 1 and n != 1 else a

    return Senales(t=t, moneda=np.full(n, moneda, np.int8), dir=rep(d, np.int8, 1), stop_pct=rep(stop_pct, np.float64, np.nan),
                   ventana=rep(ventana, np.int8, -1), clave=rep(clave, np.int32, 0),
                   prioridad=rep(prioridad, np.float64, 0.0), objetivo_r=rep(objetivo_r, np.float64, np.nan),
                   fin_ms=rep(fin_ms, np.int64, 0))


# Parámetros comunes a todas las familias (no son filtros: son el universo y los sentidos)
COMUNES = {
    "lados": Param("str", "ambos", LADOS, texto="sentidos que opera"),
    "monedas": Param("lista", list(COINS), COINS, texto="monedas"),
}


class Familia:
    nombre: str = ""                    # = nombre del archivo
    version: int = 1                    # sube con CUALQUIER cambio del archivo
    titulo: str = ""
    descripcion: str = ""
    momento: str = "continuo"           # "aperturas" | "continuo"
    datos: tuple = ("velas_1m",)        # claves de docs/07, 4.1
    historia: bool = True               # False: solo se puede medir en avance (exploratoria)
    calentamiento_d: int = 30           # días de datos antes del inicio del periodo
    parametros: dict = {}
    filtros: tuple = ()                 # filtros comunes de filtros.py que admite (cuentan para el tope de 3)
    max_senales_dia: float = 50.0       # salvaguarda: más es un error (señales por nivel en vez de por flanco)
    fuentes: tuple = ()                 # de dónde sale la idea (para la web y las propuestas)

    # ---------------------------------------------------------------- esquema
    def esquema(self) -> dict:
        from .. import filtros
        out = dict(COMUNES)
        out.update(self.parametros)
        for f in self.filtros:
            out[f] = filtros.PARAMS[f]
        return out

    def completar(self, p: dict | None) -> tuple[dict, list[str]]:
        """Parámetros completos (los que faltan, por defecto) y los errores del esquema, en español."""
        esq = self.esquema()
        p = dict(p or {})
        out, err = {}, []
        for k in p:
            if k not in esq:
                err.append(f"parámetro desconocido «{k}» (la familia {self.nombre} admite: {', '.join(sorted(esq))})")
        for k, prm in esq.items():
            v = p.get(k, prm.defecto)
            if isinstance(v, list):
                v = list(v)
            v2, e = prm.validar(v)
            if e:
                err.append(f"{k} = {v!r}: {e}")
            out[k] = v2
        return out, err

    def filtros_activos(self, p: dict) -> list[str]:
        esq = self.esquema()
        return [k for k, prm in esq.items() if prm.filtro and prm.activo(p.get(k, prm.defecto))]

    # ---------------------------------------------------------------- señales
    def senales_crudas(self, m, p: dict, desde_ms: int, hasta_ms: int) -> Senales:
        """Lo que programa cada familia: las señales antes de los parámetros comunes y los filtros."""
        raise NotImplementedError

    def senales(self, m, p: dict, desde_ms: int, hasta_ms: int) -> Senales:
        """Señales de [desde_ms, hasta_ms) con los sentidos, las monedas y los filtros comunes aplicados."""
        from .. import filtros
        p, err = self.completar(p)
        if err:
            raise ValueError("; ".join(err))
        s = self.senales_crudas(m, p, desde_ms, hasta_ms)
        if not isinstance(s, Senales):
            raise TypeError(f"{self.nombre}: senales_crudas tiene que devolver Senales")
        s = s.entre(desde_ms, hasta_ms)
        if len(s):
            mask = np.ones(len(s), bool)
            if p["lados"] == "largos":
                mask &= s.dir > 0
            elif p["lados"] == "cortos":
                mask &= s.dir < 0
            idx = {m.monedas.index(c) for c in p["monedas"] if c in m.monedas}
            mask &= np.isin(s.moneda, list(idx))
            s = s.filtrar(mask)
        if len(s) > 1:   # una decisión por moneda y minuto (la de más prioridad)
            clave = s.t * 16 + s.moneda.astype(np.int64)
            _, primera = np.unique(clave, return_index=True)
            if len(primera) < len(s):
                keep = np.zeros(len(s), bool)
                keep[primera] = True
                s = s.filtrar(keep)
        if len(s) and self.filtros:
            s = filtros.aplicar(m, s, p, self.filtros)
        return s

    def monedas_de(self, m, p: dict) -> list[tuple[int, str]]:
        """(índice, moneda) de las que hay que calcular (las del parámetro «monedas» que están en el mercado)."""
        return [(i, c) for i, c in enumerate(m.monedas) if c in (p.get("monedas") or COINS)]

    def describir(self, p: dict) -> str:
        """La regla en español llano (web y propuesta)."""
        p, _ = self.completar(p)
        esq = self.esquema()
        partes = []
        for k, prm in esq.items():
            v = p.get(k)
            if k in COMUNES and v == prm.defecto:
                continue
            if prm.filtro and not prm.activo(v):
                continue
            partes.append(f"{prm.texto or k}: {_num(v) if not isinstance(v, list) else ', '.join(v)}")
        return f"{self.titulo}" + (f" ({'; '.join(partes)})" if partes else "")

    def ficha(self) -> dict:
        """Lo que se publica para el generador (para_el_generador.json)."""
        esq = self.esquema()
        return {"nombre": self.nombre, "version": self.version, "titulo": self.titulo, "descripcion": self.descripcion,
                "momento": self.momento, "historia": self.historia, "datos": list(self.datos),
                "calentamiento_d": self.calentamiento_d, "fuentes": list(self.fuentes),
                "parametros": {k: {"tipo": v.tipo, "defecto": v.defecto, "opciones": list(v.opciones) if v.opciones else None,
                                   "min": v.min, "max": v.max, "filtro": v.filtro, "neutro": v.neutro, "texto": v.texto}
                               for k, v in esq.items()}}


# ---------------------------------------------------------------- utilidades para las familias

def flancos(cond) -> np.ndarray:
    """True solo donde la condición EMPIEZA a cumplirse."""
    c = np.asarray(cond, bool)
    if not len(c):
        return c
    return c & ~np.r_[False, c[:-1]]


def flancos_dobles(up, dn) -> tuple[np.ndarray, np.ndarray]:
    return flancos(up), flancos(dn)


def validas(b, frac: float = 0.9) -> np.ndarray:
    """Barras con al menos el 90 % de sus velas de 1 min (una barra con menos no da señal)."""
    return np.asarray(b.n_validas) >= frac * b.tf


def decision_ms(t_ms) -> np.ndarray:
    """Instante de decisión múltiplo de 1 min (hacia arriba): un dato con sello t se usa en la vela que abre en él o
    después."""
    t = np.asarray(t_ms, np.int64)
    return -(-t // MIN) * MIN


def atr_pct_en(m, moneda: str, tf: int, t_ms, n: int = 14) -> np.ndarray:
    """ATR(n) de Wilder en % de la última barra de tf cerrada en cada t (NaN sin historia)."""
    from .. import indicadores as ind
    b = m.barras(moneda, tf)
    t_ms = np.asarray(t_ms, np.int64)
    out = np.full(len(t_ms), np.nan)
    if len(b) < n:
        return out
    a = ind.atr_pct(b.h, b.l, b.c, n)
    k = np.searchsorted(b.t_cierre, t_ms, "right") - 1
    ok = k >= 0
    out[ok] = a[k[ok]]
    return out


def tendencia_diaria(m, moneda: str, t_ms) -> np.ndarray:
    """+1 alcista (cierre > SMA50 y SMA20 > SMA50 del último día UTC cerrado), −1 bajista (al revés), 0 si no."""
    from .. import indicadores as ind
    b = m.barras(moneda, 1440)
    t_ms = np.asarray(t_ms, np.int64)
    out = np.zeros(len(t_ms), np.int8)
    if len(b) < 50:
        return out
    c = np.asarray(b.c, float)
    s20, s50 = ind.sma(c, 20), ind.sma(c, 50)
    k = np.searchsorted(b.t_cierre, t_ms, "right") - 1
    ok = k >= 49
    kk = np.clip(k, 0, len(c) - 1)
    up = ok & (c[kk] > s50[kk]) & (s20[kk] > s50[kk])
    dn = ok & (c[kk] < s50[kk]) & (s20[kk] < s50[kk])
    out[up], out[dn] = 1, -1
    return out


def funding8(m, moneda: str) -> tuple[np.ndarray, np.ndarray]:
    """(t, tasa por 8 h en fracción): los periodos cortos de SOL se escalan a 8 h (medir_selectiva.funding8)."""
    ft, fr = m.funding(moneda)
    ft, fr = np.asarray(ft, np.int64), np.asarray(fr, float)
    if len(ft) < 2:
        return ft, fr
    per = np.r_[8.0, np.round(np.diff(ft) / H)]
    esc = np.where(np.isin(per, (1.0, 2.0, 4.0)), 8 / np.where(per > 0, per, 8), 1.0)
    return ft, fr * esc


def cierre_en(m, moneda: str, t_ms) -> np.ndarray:
    """Cierre de la última vela de 1 min cerrada en cada t (rejilla densa)."""
    d = m.denso(moneda)
    t_ms = np.asarray(t_ms, np.int64)
    out = np.full(len(t_ms), np.nan)
    if d is None or d.n == 0:
        return out
    k = (t_ms - d.t0) // MIN - 1
    ok = (k >= 0) & (k < d.n)
    out[ok] = np.asarray(d.c)[k[ok]]
    return out


def apertura_en(m, moneda: str, t_ms) -> np.ndarray:
    """Apertura de la vela de 1 min que abre en cada t (rejilla densa); NaN si aún no ha cerrado esa vela."""
    d = m.denso(moneda)
    t_ms = np.asarray(t_ms, np.int64)
    out = np.full(len(t_ms), np.nan)
    if d is None or d.n == 0:
        return out
    k = (t_ms - d.t0) // MIN
    ok = (k >= 0) & (k < d.n)
    out[ok] = np.asarray(d.o)[k[ok]]
    return out


def dia_semana_utc(t_ms) -> np.ndarray:
    """0 = lunes … 6 = domingo (UTC)."""
    return ((np.asarray(t_ms, np.int64) // DAY + 3) % 7).astype(np.int8)


def stop_desde(precio_ref: np.ndarray, nivel: np.ndarray, d: np.ndarray, extra_pct: float = 0.0) -> np.ndarray:
    """Distancia (%) del precio de referencia (el cierre en la decisión) al nivel del stop estructural, más un colchón.
    El libro la recalcula con el precio real de entrada (stop_pct es una distancia, no un precio)."""
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.abs(np.asarray(precio_ref, float) - np.asarray(nivel, float)) / np.asarray(precio_ref, float) * 100 + extra_pct


def en_rango_tiempo(t: np.ndarray, desde_ms: int, hasta_ms: int, margen_ms: int = 0) -> np.ndarray:
    return (t >= desde_ms - margen_ms) & (t < hasta_ms)


# ---------------------------------------------------------------- noticias (clave «noticias_3a», docs/07, 4.1)
# Las familias de noticias ven m.noticias (bot/lab_vivo/noticias.py): historias en racimo con el triaje de la IA local,
# con t = desde cuándo se pudo usar cada una (m.recortado las corta como a las velas). Si algo de aquí cambia, sube la
# versión de las familias que lo usan (noticia_racimo y noticia_apertura).

SENTIDO_MIN = 15   # |repercusion.direccion| que ya es alcista o bajista (local_ai.sentiment_of y reflejo.DIR_MIN)
FIABLE = {"n_min": 30, "dias_min": 3, "n_dia": 5, "acierto_min": 0.60, "wilson90_min": 0.50, "dia_min": 0.50,
          "z": 1.645, "horizonte_min": 240, "ventana_dias": 30, "margen_ms": 240 * MIN + 2 * H}   # bot/fiables.py
REFLEJO = {"umbral_pct": 0.5, "k_sigma": 2.0, "sigma_h": 24, "sigma_min_n": 600, "ventana_min": 60}   # bot/reflejo.py


def wilson_bajo(k, n, z: float = 1.645):
    """Cota inferior del intervalo de Wilson (fiables.wilson_bajo); con n = 0, 0."""
    k, n = np.asarray(k, float), np.asarray(n, float)
    m = np.maximum(n, 1.0)
    p = k / m
    den = 1 + z * z / m
    c = p + z * z / (2 * m)
    r = z * np.sqrt(np.maximum(p * (1 - p) / m + z * z / (4 * m * m), 0.0))
    return np.where(n > 0, (c - r) / den, 0.0)


def rango_previo(m, moneda: str, t_ms: int, minutos: int) -> tuple[float, float, float]:
    """(máximo, mínimo, fracción de velas válidas) de las velas de 1 min que abren en [t − minutos, t)."""
    d = m.denso(moneda)
    if d is None or d.n == 0:
        return np.nan, np.nan, 0.0
    b = int((t_ms - d.t0) // MIN)
    a = b - int(minutos)
    if a < 0 or b > d.n or b <= a:
        return np.nan, np.nan, 0.0
    v = np.asarray(d.valid[a:b], bool)
    if not v.any():
        return np.nan, np.nan, 0.0
    return float(np.asarray(d.h[a:b])[v].max()), float(np.asarray(d.l[a:b])[v].min()), float(v.mean())


def sigma60_pct(m, moneda: str, t_ms: int, horas: int = REFLEJO["sigma_h"]) -> float | None:
    """σ (en %) del movimiento de 60 min con los rendimientos de 1 min de las `horas` antes de t, solo minutos seguidos
    con vela (reflejo.sigma60 de una moneda); None con menos de 600."""
    d = m.denso(moneda)
    if d is None or d.n == 0:
        return None
    b = int((t_ms - d.t0) // MIN)
    a = b - horas * 60
    if a < 0 or b > d.n:
        return None
    c = np.asarray(d.c[a:b], float)
    v = np.asarray(d.valid[a:b], bool)
    ok = v[1:] & v[:-1] & (c[1:] > 0) & (c[:-1] > 0)
    if ok.sum() < REFLEJO["sigma_min_n"]:
        return None
    r = np.diff(np.log(np.where(c > 0, c, 1.0)))[ok]
    return float(np.std(r) * np.sqrt(60) * 100)


def unidades_noticias(n, monedas: tuple, generales: bool) -> tuple[np.ndarray, np.ndarray]:
    """(fila de la noticia, índice de la moneda) de cada historia×moneda (la unidad de bot/fiables.py): las monedas
    que nombra el triaje y, con generales, las cuatro para las que no nombran ninguna (cripto en general y macro)."""
    filas, mons = [], []
    for k in range(len(monedas)):
        sel = np.flatnonzero(n.de_moneda(k, generales))
        filas.append(sel)
        mons.append(np.full(len(sel), k, np.int64))
    if not filas:
        return np.zeros(0, np.int64), np.zeros(0, np.int64)
    f, c = np.concatenate(filas), np.concatenate(mons)
    o = np.lexsort((c, f))
    return f[o], c[o]


def fiables_por_dia(dia, grupo, acierto, G: int, d0: int, d1: int, c: dict = FIABLE) -> np.ndarray:
    """ok[L − d0, g]: ¿el grupo g es «fiable» con los criterios fijados de bot/fiables.py, con los casos de los días UTC
    [L − ventana + 1, L]? (n ≥ 30; ≥ 3 días con ≥ 5; acierto ≥ 60 %; Wilson al 90 % ≥ 50 %; ≥ 50 % en cada semana
    UTC con ≥ 5.) dia: día UTC de cada caso (ms // DAY); grupo: 0..G−1 (−1, sin grupo); acierto: 0/1."""
    out = np.zeros((max(0, d1 - d0 + 1), max(0, G)), bool)
    if not len(dia) or G <= 0:
        return out
    dia = np.asarray(dia, np.int64)
    o = np.argsort(dia, kind="stable")
    dia, grupo, acierto = dia[o], np.asarray(grupo, np.int64)[o], np.asarray(acierto, float)[o]
    W = int(c["ventana_dias"])
    for L in range(d0, d1 + 1):
        i = int(np.searchsorted(dia, L - W + 1, "left"))
        j = int(np.searchsorted(dia, L, "right"))
        if j - i < c["n_min"]:
            continue
        g, h, dd = grupo[i:j], acierto[i:j], dia[i:j]
        mk = g >= 0
        g, h, dd = g[mk], h[mk], dd[mk]
        if not len(g):
            continue
        n = np.bincount(g, minlength=G).astype(float)
        k = np.bincount(g, weights=h, minlength=G)
        nd = np.bincount(g * W + (dd - (L - W + 1)), minlength=G * W).reshape(G, W)
        ndias = (nd >= c["n_dia"]).sum(axis=1)
        su, si = np.unique((dd + 3) // 7, return_inverse=True)   # semanas UTC de lunes a domingo
        P = len(su)
        npd = np.bincount(g * P + si, minlength=G * P).reshape(G, P).astype(float)
        kpd = np.bincount(g * P + si, weights=h, minlength=G * P).reshape(G, P)
        with np.errstate(divide="ignore", invalid="ignore"):
            frac = np.where(npd >= c["n_dia"], kpd / np.maximum(npd, 1.0), np.inf)
        constante = frac.min(axis=1) >= c["dia_min"] - 1e-12
        p = k / np.maximum(n, 1.0)
        out[L - d0] = ((n >= c["n_min"]) & (ndias >= c["dias_min"]) & (p >= c["acierto_min"] - 1e-12)
                       & (wilson_bajo(k, n, c["z"]) >= c["wilson90_min"] - 1e-12) & constante)
    return out


def grupos_fiables(m, n, monedas: tuple, generales: bool, d0: int, d1: int) -> tuple[np.ndarray, int]:
    """Tabla de fiables_por_dia de los grupos tipo×sentido (los de bot/fiables.py) con lo que se sabía cada día:
    acierto = sentido × (rendimiento a 4 h desde t − media de ese rendimiento de las historias de esa moneda y día UTC)
    > 0. Devuelve (tabla, nº de tipos); el grupo de una noticia es tipo × 2 + (sentido > 0)."""
    T = len(n.tipos) or int(n.tipo.max(initial=0)) + 1
    f, k = unidades_noticias(n, monedas, generales)
    sen = n.sentido()[f]
    keep = sen != 0
    f, k, sen = f[keep], k[keep], sen[keep]
    if not len(f):
        return np.zeros((max(0, d1 - d0 + 1), 2 * T), bool), T
    t = n.t[f]
    r = np.full(len(f), np.nan)
    for ci, c in enumerate(monedas):
        sel = k == ci
        if sel.any():
            p0, p1 = cierre_en(m, c, t[sel]), cierre_en(m, c, t[sel] + FIABLE["horizonte_min"] * MIN)
            with np.errstate(divide="ignore", invalid="ignore"):
                r[sel] = p1 / p0 - 1
    ok = np.isfinite(r)
    f, k, sen, t, r = f[ok], k[ok], sen[ok], t[ok], r[ok]
    dia = t // DAY
    clave = k * 1_000_000 + dia
    u, inv = np.unique(clave, return_inverse=True)
    deriva = (np.bincount(inv, weights=r) / np.bincount(inv))[inv]
    hit = (sen * (r - deriva) > 0).astype(float)
    g = n.tipo[f].astype(np.int64) * 2 + (sen > 0)
    return fiables_por_dia(dia, g, hit, 2 * T, d0, d1), T


def ultimo_dia_completo(t_ms) -> np.ndarray:
    """Último día UTC cuyos casos ya se habían medido en t: han pasado sus 4 h de medida y 2 h de margen (fiables)."""
    return (np.asarray(t_ms, np.int64) - FIABLE["margen_ms"]) // DAY - 1


# Medios por su acierto hasta cada fecha (30-10-2026, lo pidió el usuario: «si hay medios que aciertan menos de un 50 %
# de las veces, FUERA»). Solo con sus historias de días UTC completos anteriores (como fiables); acierto ajustado a 4 h.
MEDIOS = {"casos_min": 20, "dias_min": 10, "malo_max": 0.50, "bueno_min": 0.60, "bueno_wilson_min": 0.50, "z": 1.645}
MEDIOS_MODOS = ("todos", "sin_malos", "solo_buenos")


def mascara_medios(m, n, monedas: tuple, generales: bool, modo: str) -> np.ndarray:
    """¿Cuenta cada noticia según el acierto de su medio (el de su primera pieza) hasta el último día completo antes de
    poder usarla? «sin_malos»: fuera si el medio tiene muestra (≥ 20 historias en ≥ 10 días) y la cota ALTA de Wilson
    al 90 % < 50 %; sin muestra, cuenta. «solo_buenos»: solo si tiene muestra, acierto ≥ 60 % y cota baja ≥ 50 %.
    El acierto de una historia es la media de sus aciertos ajustados a 4 h por moneda (la unidad de fiables)."""
    if modo == "todos" or not len(n):
        return np.ones(len(n), bool)
    f, k = unidades_noticias(n, monedas, generales)
    sen = n.sentido()[f]
    ok = sen != 0
    f, k, sen = f[ok], k[ok], sen[ok]
    t = n.t[f]
    r = np.full(len(f), np.nan)
    for ci, c in enumerate(monedas):
        sel = k == ci
        if sel.any():
            with np.errstate(divide="ignore", invalid="ignore"):
                r[sel] = cierre_en(m, c, t[sel] + FIABLE["horizonte_min"] * MIN) / cierre_en(m, c, t[sel]) - 1
    ok = np.isfinite(r)
    f, k, sen, t, r = f[ok], k[ok], sen[ok], t[ok], r[ok]
    if len(f):
        _, inv = np.unique(k * 1_000_000 + t // DAY, return_inverse=True)
        der = (np.bincount(inv, weights=r) / np.bincount(inv))[inv]
        hit = (sen * (r - der) > 0).astype(float)
        uf, invf = np.unique(f, return_inverse=True)            # una historia, un caso
        h_hist = np.bincount(invf, weights=hit) / np.bincount(invf)
        med, dia = n.medio[uf].astype(np.int64), n.t[uf] // DAY
    else:
        med = dia = h_hist = np.zeros(0)
    # por medio y día, sumas acumuladas: lo de cada noticia con los días ≤ su último día completo
    o = np.lexsort((dia, med))
    clave = med[o] * 1_000_000 + dia[o]
    ck = np.r_[0.0, np.cumsum(h_hist[o])]
    nuevo = np.r_[True, clave[1:] != clave[:-1]] if len(clave) else np.zeros(0, bool)
    cd = np.r_[0, np.cumsum(nuevo)]
    mi = n.medio.astype(np.int64)
    a = np.searchsorted(clave, mi * 1_000_000, "left")
    j = np.searchsorted(clave, mi * 1_000_000 + ultimo_dia_completo(n.t), "right")
    nn = (j - a).astype(float)
    kk = ck[j] - ck[a]
    muestra = (nn >= MEDIOS["casos_min"]) & (cd[j] - cd[a] >= MEDIOS["dias_min"])
    if modo == "sin_malos":
        return ~(muestra & (1 - wilson_bajo(nn - kk, nn, MEDIOS["z"]) < MEDIOS["malo_max"]))
    with np.errstate(divide="ignore", invalid="ignore"):
        return muestra & (kk / np.maximum(nn, 1) >= MEDIOS["bueno_min"]) &             (wilson_bajo(kk, nn, MEDIOS["z"]) >= MEDIOS["bueno_wilson_min"])

