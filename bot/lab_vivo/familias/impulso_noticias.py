"""Familia «impulso_noticias»: las entradas del motor de aperturas (apertura_impulso tal cual: «6 diarias» y las demás
variantes del impulso) con el filtro de las noticias de 3 años de las horas previas (filtros.noticias_previas): no
entra si las noticias de esa moneda (y las generales) van en contra, o entra solo si van a favor. Es lo que pidió el
usuario el 30-10-2026: usar las noticias para reforzar las estrategias que ya van mejor. Datos: noticias_3a.

La entrada es la de apertura_impulso sin tocar nada: esta familia la hereda. Para que un cambio de aquella no cambie en
silencio los resultados de esta, se fija su versión y la firma de su archivo (sin retornos de carro): si cambian, esta
no se carga hasta que se suba su versión."""
import hashlib
from pathlib import Path

from .apertura_impulso import AperturaImpulso

BASE_VERSION = 1
BASE_SHA = "ea984bfd78e2c4d3"   # sha1 (16) de apertura_impulso.py sin «\r», el 30-10-2026


def _sha_base() -> str:
    return hashlib.sha1((Path(__file__).parent / "apertura_impulso.py").read_bytes().replace(b"\r", b"")).hexdigest()[:16]


if AperturaImpulso.version != BASE_VERSION or _sha_base() != BASE_SHA:
    raise ImportError("apertura_impulso cambió: revisa impulso_noticias y sube su versión (y BASE_VERSION y BASE_SHA)")


class ImpulsoNoticias(AperturaImpulso):
    nombre = "impulso_noticias"
    version = 1
    titulo = "Impulso de las aperturas con el filtro de las noticias de las horas previas"
    descripcion = ("Las entradas del motor de aperturas (apertura_impulso) que no van contra las noticias de las horas "
                   "previas, o solo las que van a su favor (noticias de 3 años con el triaje de la IA local).")
    datos = ("velas_1m", "aperturas", "noticias_3a")
    fuentes = ("La operativa del usuario (motor de aperturas, news_mode «no_contra» y «a_favor»)",
               "Tetlock (2007), Giving Content to Investor Sentiment, Journal of Finance 62(3)",
               "Lopez-Lira y Tang (2023-2025), Can ChatGPT Forecast Stock Price Movements? (arXiv 2304.07619)")
    filtros = ("noticias_previas", "noticias_horas", "noticias_imp_min", "noticias_generales", "noticias_medios")
