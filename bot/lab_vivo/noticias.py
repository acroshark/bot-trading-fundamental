"""Noticias del laboratorio vivo (docs/07, 4.1, clave «noticias_3a»): las historias del histórico de 3 años
(bot/noticias_historia.py: GDELT y los 24 medios de cripto, deduplicadas como la mesa) que llegaron a racimo y tienen el
triaje de la IA local del bot, como arrays de numpy ordenados por t.

t es desde cuándo se puede usar cada una: el sello del medio que completa el racimo más el minuto del triaje. Una familia
solo ve las de t < hasta del Mercado (Mercado.recortado), como las velas: así la prueba del recorte las cubre igual.

El archivo lo escribe `python -m bot.noticias_historia lab` (noticias_historia/lab/noticias.npz y meta.json); sin él,
Mercado.noticias es None y las familias que las usan no dan señales (la admisión lo dice: A6)."""
import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path

import numpy as np

from .. import config

DIR = Path(os.environ.get("BOT_NOTICIAS_HISTORIA_DIR", str(config.DATA_DIR / "noticias_historia"))) / "lab"
ARCHIVO, META = "noticias.npz", "meta.json"
CLASES = ("cripto", "macro")
FUENTES = ("gdelt", "wp")
UMBRAL_SENTIDO = 15   # local_ai.sentiment_of: alcista si direccion > 15, bajista si < −15
COLUMNAS = {   # nombre → tipo
    "t": np.int64,     # desde cuándo se puede usar (ms UTC, múltiplo de 60.000); orden creciente
    "t0": np.int64,    # primera publicación de la historia
    "n30": np.int16,   # medios distintos en los 30 min desde la primera publicación
    "nmed": np.int16,  # medios distintos en las 3 h de la ventana de la mesa
    "clase": np.int8,  # índice en CLASES
    "mon": np.int8,    # bits de las monedas del triaje (1 << índice en config.COINS); 0 = ninguna (general o macro)
    "dir": np.int16,   # repercusion.direccion (−100 a 100)
    "imp": np.int8,    # importancia (0 a 10)
    "tipo": np.int8,   # índice en meta["tipos"] (local_ai.TIPOS)
    "cred": np.int8,   # credibilidad (0 a 100)
    "nov": np.int8,    # novedad (0 a 100)
    "mag": np.int8,    # repercusion.magnitud (0 a 100)
    "fuente": np.int8,  # índice en FUENTES (la de la primera pieza)
    "medio": np.int32,  # índice en meta["medios"] (el de la primera pieza)
}


@dataclass
class Noticias:
    t: np.ndarray
    t0: np.ndarray
    n30: np.ndarray
    nmed: np.ndarray
    clase: np.ndarray
    mon: np.ndarray
    dir: np.ndarray
    imp: np.ndarray
    tipo: np.ndarray
    cred: np.ndarray
    nov: np.ndarray
    mag: np.ndarray
    fuente: np.ndarray
    medio: np.ndarray
    tipos: tuple = ()
    medios: tuple = ()
    meta: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.t)

    @staticmethod
    def vacias(tipos=(), medios=()) -> "Noticias":
        return Noticias(**{k: np.zeros(0, tp) for k, tp in COLUMNAS.items()}, tipos=tuple(tipos), medios=tuple(medios))

    @staticmethod
    def de_filas(filas: list[dict], tipos, medios=(), meta: dict | None = None) -> "Noticias":
        """Desde dicts con las claves de COLUMNAS (las que falten, 0), ordenadas por t (las pruebas y el compilador)."""
        filas = sorted(filas, key=lambda r: (int(r["t"]), int(r.get("t0", r["t"]))))
        cols = {k: np.array([r.get(k, 0) for r in filas], dtype=tp) if filas else np.zeros(0, tp)
                for k, tp in COLUMNAS.items()}
        return Noticias(**cols, tipos=tuple(tipos), medios=tuple(medios), meta=dict(meta or {}))

    def tramo(self, desde: int | None, hasta: int | None) -> "Noticias":
        """Las de desde ≤ t < hasta (None: sin límite)."""
        i = 0 if desde is None else int(np.searchsorted(self.t, desde, "left"))
        j = len(self.t) if hasta is None else int(np.searchsorted(self.t, hasta, "left"))
        if i == 0 and j == len(self.t):
            return self
        return Noticias(**{k: getattr(self, k)[i:j] for k in COLUMNAS}, tipos=self.tipos, medios=self.medios,
                        meta=self.meta)

    def sentido(self) -> np.ndarray:
        """+1 alcista, −1 bajista, 0 neutral (local_ai.sentiment_of)."""
        return np.where(self.dir > UMBRAL_SENTIDO, 1, np.where(self.dir < -UMBRAL_SENTIDO, -1, 0)).astype(np.int8)

    def de_moneda(self, k: int, generales: bool = False) -> np.ndarray:
        """Máscara de las que nombran la moneda k (índice en config.COINS) y, con generales, también las que no nombran
        ninguna (cripto en general y macro)."""
        m = ((self.mon.astype(np.int16) >> k) & 1).astype(bool)
        return m | (self.mon == 0) if generales else m

    def indice_tipo(self, nombre: str) -> int:
        return self.tipos.index(nombre) if nombre in self.tipos else -1

    def cobertura(self, desde_ms: int, hasta_ms: int) -> float:
        """Fracción de los días naturales de [desde, hasta) con alguna noticia (para A6)."""
        DAY = 86_400_000
        dias = max(1, (hasta_ms - desde_ms) // DAY)
        x = self.tramo(desde_ms, hasta_ms)
        return float(len(np.unique(x.t // DAY))) / dias if len(x) else 0.0


def firma(carpeta: Path | None = None) -> str | None:
    """La firma del contenido (la escribe el compilador en meta.json); None si no hay archivo."""
    carpeta = Path(carpeta or DIR)
    try:
        return json.loads((carpeta / META).read_text("utf-8")).get("firma")
    except (OSError, ValueError):
        return None


def guardar(n: Noticias, carpeta: Path, meta: dict | None = None) -> None:
    carpeta = Path(carpeta)
    carpeta.mkdir(parents=True, exist_ok=True)
    tmp = carpeta / (ARCHIVO + ".tmp.npz")
    np.savez(tmp, **{k: getattr(n, k) for k in COLUMNAS})
    os.replace(tmp, carpeta / ARCHIVO)
    m = {**n.meta, **(meta or {}), "tipos": list(n.tipos), "medios": list(n.medios)}
    (carpeta / (META + ".tmp")).write_text(json.dumps(m, ensure_ascii=False), "utf-8")
    os.replace(carpeta / (META + ".tmp"), carpeta / META)


def cargar(carpeta: Path | None = None) -> Noticias | None:
    """Las del archivo compilado, o None si no hay (o no se puede leer)."""
    carpeta = Path(carpeta or DIR)
    try:
        meta = json.loads((carpeta / META).read_text("utf-8"))
        with np.load(carpeta / ARCHIVO) as z:
            cols = {k: np.asarray(z[k], dtype=tp) for k, tp in COLUMNAS.items()}
    except (OSError, ValueError, KeyError):
        return None
    return Noticias(**cols, tipos=tuple(meta.get("tipos") or ()), medios=tuple(meta.get("medios") or ()), meta=meta)


assert [f.name for f in fields(Noticias)][:len(COLUMNAS)] == list(COLUMNAS)
