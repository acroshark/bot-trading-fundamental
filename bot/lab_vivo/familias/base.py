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
