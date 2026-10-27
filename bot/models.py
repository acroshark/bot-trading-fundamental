"""Tipos y utilidades comunes."""
import hashlib
import time
from dataclasses import dataclass, field


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Item:
    """Una pieza recién leída de una fuente, antes de filtrarla."""
    source: str
    uid: str
    title: str
    url: str = ""
    published: int | None = None          # ms desde epoch; None si la fuente no da hora
    coins_hint: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)   # tickers que menciona la fuente, del universo o no
    tier: str | None = None               # sustituye al nivel de la fuente (p. ej. Tree News reenvía tuits)
    via: str | None = None                # subfuente dentro de un agregador
    region: str | None = None             # región de noticias (clave de regions.REGIONS); None = global
    lang: str | None = None               # idioma del titular (ja, zh, ko, de, en, es, pt...)
    outlet: str | None = None             # medio que la firma (dominio o nombre): dos feeds del mismo medio no se confirman


@dataclass
class Source:
    id: str
    name: str
    tier: str
    interval: float
    fetch: object                         # async (ClientSession) -> list[Item]


def story_id(source: str, uid: str) -> str:
    return "n" + hashlib.sha1(f"{source}|{uid}".encode()).hexdigest()[:12]


def fmt_age(ms: int | None) -> str:
    if ms is None:
        return "hora desconocida"
    s = ms / 1000
    if s < 1:
        return f"+{int(ms)} ms"
    if s < 60:
        return f"+{s:.1f} s".replace(".", ",")
    if s < 3600:
        return f"+{int(s // 60)} min {int(s % 60):02d} s"
    return f"+{int(s // 3600)} h {int(s % 3600 // 60):02d} min"
