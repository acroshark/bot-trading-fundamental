"""Registro de familias del laboratorio vivo (docs/07, 11.8): se descubren escaneando esta carpeta EN CADA
SUBPROCESO, así que una familia nueva no necesita reiniciar nada y una que falla solo tumba sus propias tareas.

Cada archivo <nombre>.py define una clase que hereda de base.Familia con nombre == <nombre>. La firma de una familia
es el sha1 de su archivo: la tabla «familias» de lab_vivo.db guarda (nombre, versión, sha) y una familia cuyo archivo
cambia sin subir la versión no se usa (sus resultados no serían comparables)."""
import hashlib
import importlib
import logging
from pathlib import Path

from .base import Familia

log = logging.getLogger("lab_vivo.familias")
CARPETA = Path(__file__).resolve().parent
NO_FAMILIAS = {"__init__", "base"}
_REG: dict[str, Familia] = {}
_ERR: dict[str, str] = {}


def sha_archivo(nombre: str) -> str:
    p = CARPETA / f"{nombre}.py"
    return hashlib.sha1(p.read_bytes()).hexdigest()[:16] if p.exists() else ""


def nombres() -> list[str]:
    return sorted(p.stem for p in CARPETA.glob("*.py") if p.stem not in NO_FAMILIAS and not p.stem.startswith("_"))


def cargar(forzar: bool = False) -> dict[str, Familia]:
    """{nombre: instancia} de las familias que se importan bien; los fallos quedan en errores()."""
    if _REG and not forzar:
        return _REG
    _REG.clear()
    _ERR.clear()
    for n in nombres():
        try:
            mod = importlib.import_module(f"{__name__}.{n}")
            if forzar:
                mod = importlib.reload(mod)
            cls = [v for v in vars(mod).values() if isinstance(v, type) and issubclass(v, Familia) and v is not Familia
                   and getattr(v, "nombre", None) == n]
            if not cls:
                raise TypeError(f"el archivo {n}.py no define una Familia con nombre «{n}»")
            _REG[n] = cls[0]()
        except Exception as e:   # una familia rota no tumba a las demás
            _ERR[n] = f"{type(e).__name__}: {e}"
            log.warning("Familia %s no se puede cargar: %s", n, _ERR[n])
    return _REG


def errores() -> dict[str, str]:
    cargar()
    return dict(_ERR)


def obtener(nombre: str) -> Familia:
    reg = cargar()
    if nombre not in reg:
        if nombre in _ERR:
            raise KeyError(f"la familia {nombre} no se puede cargar: {_ERR[nombre]}")
        raise KeyError(f"familia desconocida: {nombre} (registradas: {', '.join(sorted(reg))})")
    return reg[nombre]


def firmas() -> dict[str, dict]:
    return {n: {"version": f.version, "sha": sha_archivo(n)} for n, f in cargar().items()}
