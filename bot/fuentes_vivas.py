"""Fuentes vivas: ciclo de vida de cada medio (prueba → activa → silenciada → retirada) y descubrimiento de medios nuevos.

Estado. La tabla source_status manda sobre el registro bot/fuentes/*.json (no se reescriben los JSON para esto): un feed
del registro sin fila está «activa». Cada medio (dominio que firma las noticias) tiene además su fila «medio:<dominio>»,
llegue por su feed propio, por Google News o por un relay: su estado vale para TODOS sus canales (FeedPoller.
set_outlet_estado). feeds.FeedPoller lo respeta: una retirada no se sondea, una silenciada se sondea menos y sus noticias
llegan a la mesa con peso 0, una en prueba se sondea normal con peso 0,5.

Medio y canal. El acierto y el ruido se juzgan por MEDIO (con todos sus canales). Un feed es un canal: solo se le aplican
las reglas técnicas y la de ruido (una consulta de Google News o una categoría que solo trae ruido), nunca la de acierto,
porque una consulta mezcla muchos medios y silenciarla por acierto callaría también a los buenos.

Medida. Cada noticia que la mesa capta de un feed (gancho FeedPoller.on_item) o de un relay (eventos «captured» de Tree)
queda en fv_noticias con su feed, su medio, su hora y su latencia. A las 6, 24 y 96 h recibe un veredicto
(acierto | fallo | ruido | util), por este orden:
  - el de veredictos() si se inyecta (ver «Interfaces»);
  - el de las medidas del aprendizaje (news_outcomes de bot/aprendizaje.py, mismo umbral), AJUSTADO AL MERCADO con su
    mercado_base: se guarda qué parte de sus medidas fue a favor (ac) y qué habría acertado la tendencia del día (base);
    si todo cae, acertar «bajista» no tiene mérito;
  - si aún no hay: «ruido» si el triaje la archivó (lowprio) y «util» si la trió;
y se apunta si llegó a triarse (la cobertura: solo se mide lo que la IA local trió) y si otros medios la confirmaron
después (la dio antes que nadie). El acierto mide el medio y la lectura del triaje juntos: la dirección la pone la IA.

Reglas (revisar(), cada hora). HONESTIDAD ESTADÍSTICA: las noticias de una misma hora comparten mercado (y las 4 monedas
se mueven juntas), así que cuentan como UN caso independiente; los mínimos piden casos independientes repartidos en al
menos 7 días distintos. Las cifras cuentan desde el día siguiente al último cambio de estado (sin cambios, los últimos
120 días), con decaimiento (un caso de hace 30 días pesa la mitad) y contracción bayesiana hacia lo neutro:
  - silenciar un medio: ≥ 30 casos con dirección y acierto ajustado y contraído claramente bajo lo neutro (techo del 90 %
    < 50 % y media ≤ 45 %); medio o feed: ≥ 30 casos y ruido contraído ≥ 90 %; feed: caída técnica (≥ 10 fallos
    seguidos durante ≥ 72 h de bot en marcha, salvo que falle más del 30 % de los feeds, que entonces es nuestra red).
    Como mucho 3 silencios o retiradas automáticas por revisión y 10 en 24 h: el resto queda como sugerencia.
  - restaurar: desde que se silenció, ≥ 20 casos sin ruido y sin acierto bajo lo neutro; o vuelve a responder.
  - retirar: tras ≥ 30 días silenciada, solo si en ese tiempo VUELVE a cumplirse la condición de silencio (con sus
    mínimos) o sigue caída. Sin datos suficientes sigue silenciada; a los 90 días, si tuvo material y aun así no juntó
    los casos, se retira «por falta de datos, no por malos resultados». Nunca solas las «pocos_datos», ni las que se
    quedaron sin triaje suficiente (dio noticias pero la IA no llegó a triarlas).
  - activar una «prueba»: ≥ 20 casos sin malos resultados; si en 60 días no los junta porque publica poco, se silencia
    («pocos_datos»); si es porque no se triaron, sigue en prueba («sin triaje suficiente»).
  Las oficiales (T0) solo se silencian por caída técnica o a mano. Lo que decides tú a mano (también al deshacer) se
  respeta 30 días; después vuelven a mandar los datos recogidos desde tu decisión, con los mismos mínimos (una caída
  técnica cuenta siempre). Cada cambio queda en source_status_log y en evolution_log (inyectable) con su motivo y sus
  cifras, y se deshace volviendo EXACTAMENTE al estado de antes (acciones activar, a_prueba, silenciar, retirar). Este
  módulo no toca las reglas de operación del usuario (config.USER_RULES): solo decide qué noticias cuentan.

Descubrimiento. Medios de agregadores y relays que no están en el registro y que en 30 días dan ≥ 10 casos medidos sin
ser ruido, y además las dan antes que nadie (≥ 3 confirmadas después por otros) o aciertan (contraído ≥ 55 %). Se busca su
RSS (<link rel="alternate"> de la portada y rutas típicas), se verifica (descarga real, RSS/Atom válido, ≥ 3 entradas, la
última de hace menos de 7 días, ≥ 2 en 14 días y que enlacen al propio medio) y se añade en «prueba» a su región: en
marcha al FeedPoller, en la base y en bot/fuentes/<región>.json. Con el automático apagado (BOT_FUENTES_AUTO=0) se busca
y verifica igual, pero queda «verificado, pendiente de alta» hasta que lo añadas tú (acción «anadir»). Como mucho 12
peticiones por hora con 2 s entre ellas, robots.txt respetado, 401/403/429 cuentan como bloqueo (no se insiste en 30
días), identificador honesto del bot (config.USER_AGENT) y sin credenciales.

Interfaces inyectables:
  veredictos(stories: list[str]) -> dict[str, str | None]   # opcional; se llama en un HILO (conexión propia)
      valores: "acierto" | "fallo" | "ruido" | "util" | None (sin medir aún). «util» = sirve pero sin dirección medible.
  registrar(entrada: dict) -> None                            # se llama en el bucle; rápido y sin red
      entrada = {ts, area: "fuentes", accion, id, nombre, antes, despues, tipo, motivo, cifras, auto, por,
                 reversible: True, deshacer: {accion, id}}. evolucion.Evolucion.conectar_fuentes(fv) lo engancha al
      evolution_log y deshace con fv.accion().
  prioridad_triaje(prio, estado, story) -> float | None       # para la mesa: cupo de muestreo de silenciadas y pruebas
"""
import asyncio
import hashlib
import html
import json
import logging
import math
import os
import re
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import aiohttp

from . import config, hunters, importar_fuentes, regions
from .feeds import ESTADOS, GOOGLE_NEWS_HOST, PESO_ESTADO, REDIRECT_SITES, FeedSpec, _host, _looks_like_feed, site
from .models import now_ms, story_id

log = logging.getLogger("fuentes_vivas")
try:   # el mismo umbral que el aprendizaje: dentro de él, el movimiento es ruido de mercado
    from .aprendizaje import PARAMS as _APR
    UMBRAL_PCT = float(_APR.get("umbral_pct", 0.3))
except Exception:   # sin el módulo (o roto) no se para nada
    UMBRAL_PCT = 0.3

AUTO = os.environ.get("BOT_FUENTES_AUTO", "1") != "0"          # 0: solo sugiere, no cambia nada
DESCUBRIR = os.environ.get("BOT_FUENTES_DESCUBRIR", "1") != "0"
MEDIO = "medio:"
HOST_RX = re.compile(r"(?=.{4,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+")
H = 3_600_000
DAY = 86_400_000

# --- Medida
VEREDICTOS = ("acierto", "fallo", "ruido", "util")
MEDIR_H = (6, 24, 96)        # horas tras la captura a las que se mide; tras la última queda cerrada
MEDIR_MAX = 20_000           # noticias por ronda (si hay más, siguen en la siguiente)
LOTE = 500
VENTANA_DIAS = 120           # lo que se guarda y se mide
VIDA_MEDIA_DIAS = 30         # decaimiento: un caso de hace 30 días pesa la mitad
BUF_MAX = 50_000

# --- Reglas (honestidad estadística: sin muestra suficiente no se concluye nada)
NEUTRO = 0.5
PRIOR_K = 20                 # contracción del acierto: 20 casos ficticios al 50 %
K_RUIDO = 10                 # contracción del ruido hacia el ruido medio de todas las fuentes
Z_CLARO = 1.645              # «claramente por debajo»: el techo del 90 % (una cola) queda bajo lo neutro
MIN_CASOS = 30               # casos INDEPENDIENTES (horas distintas): las noticias de una misma hora comparten mercado
MIN_DIAS = 7                 # y repartidos en al menos 7 días distintos: un mal día de mercado no decide nada
ACIERTO_MAX = 0.45           # además, una diferencia que importe
RUIDO_MAX = 0.90
RUIDO_RESTAURAR = 0.80
MIN_CASOS_ACTIVAR = 20
MIN_CASOS_RESTAURAR = 20
MIN_DIR = 10                 # casos con dirección para que el acierto cuente al activar o restaurar
RETIRAR_DIAS = 30            # silenciada al menos esto y con la condición de silencio confirmada otra vez → retirada
SILENCIO_MAX_DIAS = 90       # sin datos para decidir, sigue silenciada hasta aquí (luego, retirada por falta de datos)
PRUEBA_MAX_DIAS = 60         # una prueba que en 60 días no junta los casos (y no es por el triaje) se silencia
MANUAL_DIAS = 30             # lo que cambias a mano se respeta 30 días; luego mandan los datos desde tu cambio
MAX_BAJADAS_REVISION = 3     # silencios y retiradas automáticas (no técnicas) por revisión; el resto, sugerencias
MAX_BAJADAS_DIA = 10         # y en 24 h
TEC_MIN_FALLOS = 10
TEC_HORAS = 72
TEC_MAX_RED = 0.30
PROTEGIDAS = ("T0",)         # oficiales: solo se silencian por caída técnica o a mano
# Cupo de triaje para silenciadas y pruebas: 1 de cada 4 de sus noticias entra en la cola de la IA local con bonus de
# nivel (como una T0); sin él, una silenciada casi nunca se triaría y no podría demostrar que mejora.
MUESTREO_N = 4
BONO_MUESTRA = 1.5
PRIO_SILENCIADA = -6.0       # el resto de las silenciadas, al final de la cola
AVISO = ("Cada hora con noticias es UN caso: las de una misma hora comparten mercado y las cuatro monedas se mueven "
         "juntas. Con pocos casos, o todos de pocos días, no se concluye nada. El acierto mide el medio y la lectura del "
         "triaje juntos (la dirección la pone la IA local) y está ajustado a lo que hizo el mercado ese día. Solo se mide "
         "lo que llegó a triarse: la cobertura dice qué parte fue. Solo paper.")

# --- Descubrimiento
CAND_DIAS = 30
CAND_MIN_CASOS = 10
CAND_MIN_PRIMERAS = 3
CAND_ACIERTO_MIN = 0.55
MAX_EN_PRUEBA = 100          # descubiertas en prueba a la vez
TIER_NUEVA = "T3"
PETICIONES_HORA = 12
MIN_PRESUPUESTO = 3          # robots, portada y al menos un feed
PAUSA_S = 2.0                # entre peticiones del descubrimiento
REINTENTO_DIAS = 7
BLOQUEO_DIAS = 30
MAX_INTENTOS = 3
MAX_PRUEBAS = 6              # URLs de feed probadas por medio y ronda
RUTAS_RSS = ("/rss", "/feed", "/rss.xml", "/feed.xml", "/atom.xml", "/index.xml")
BLOQUEO = (401, 403, 429, 451)
FEED_MIN_ENTRADAS = 3
FEED_RECIENTE_DIAS = 7
DESCARGA_MAX = 3_000_000
TIMEOUT = aiohttp.ClientTimeout(total=20)
ACCEPT = "text/html, application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5"
RELAYS = ("tree",)           # cazadores que reenvían a otros medios (su enlace dice el medio)
# Redes sociales y plataformas: no tienen un RSS de medio que añadir.
NO_RSS = {"x.com", "twitter.com", "t.me", "telegram.org", "reddit.com", "youtube.com", "youtu.be", "facebook.com",
          "instagram.com", "tiktok.com", "linkedin.com", "google.com", "medium.com", "substack.com", "threads.net"}
# Medios que ya leen los cazadores directos (bot/hunters.py).
HUNTER_SITES = {"coindesk.com", "cointelegraph.com", "theblock.co", "decrypt.co", "bitcoinmagazine.com", "federalreserve.gov",
                "bls.gov", "sec.gov", "europa.eu", "binance.com", "okx.com", "upbit.com", "coinbase.com", "kraken.com",
                "treeofalpha.com"}
LINK_RX = re.compile(rb"<link\b[^>]*>", re.I)
ATTR_RX = re.compile(rb"""([a-zA-Z_:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")
FEED_TYPES = (b"application/rss+xml", b"application/atom+xml", b"application/rdf+xml")
TITLE_RX = re.compile(rb"<title[^>]*>(.*?)</title>", re.I | re.S)
FV_COLS = "story, ts, feed, medio, nombre, host, region, lang, tier, lat_s, veredicto, confirmada, medir_en"
ACCION_TXT = {"silenciar": "Silenciada", "retirar": "Retirada", "restaurar": "Restaurada", "activar": "Activada",
              "a_prueba": "Vuelta a prueba", "anadir": "Añadida en prueba"}
CAND_TXT = {"pendiente": "Pendiente de buscar su RSS", "verificado": "RSS verificado: pendiente de que lo añadas",
            "anadido": "Añadido en prueba", "sin_rss": "Sin RSS válido", "bloqueado": "Bloqueado (robots o el servidor)",
            "en_registro": "Ya estaba en el registro", "error": "Fallo de red: se reintentará"}
# Acciones a mano (web y deshacer): estado nuevo (None = el que tenía antes de silenciarse), desde qué estados y texto.
MANUAL = {"restaurar": (None, ("silenciada", "retirada"), "Restaurada a mano"),
          "silenciar": ("silenciada", ("activa", "prueba", "retirada"), "Silenciada a mano"),
          "retirar": ("retirada", ("activa", "prueba", "silenciada"), "Retirada a mano"),
          "activar": ("activa", ("prueba", "silenciada", "retirada"), "Activada a mano"),
          "a_prueba": ("prueba", ("activa", "silenciada", "retirada"), "Vuelta a prueba a mano")}
# Deshacer un cambio = volver exactamente al estado de antes (sin estado antes = era un alta: se retira). Un silencio o
# una retirada desde activa o prueba se deshace con «restaurar» (ver _deshacer).
DESHACER = {"activa": "activar", "prueba": "a_prueba", "silenciada": "silenciar", "retirada": "retirar"}


def _pct(x: float) -> str:
    return f"{round(x * 100)} %"


def _n(x: float, d: int = 1) -> str:
    return f"{x:.{d}f}".replace(".", ",")


# ---------- Estadística (funciones puras) ----------
def contraido(aciertos: float, fallos: float, k: float = PRIOR_K, p0: float = NEUTRO) -> tuple[float, float]:
    """Acierto contraído hacia lo neutro (beta con k casos ficticios a p0) y su desviación típica."""
    n = aciertos + fallos
    m = (aciertos + k * p0) / (n + k)
    return m, math.sqrt(m * (1 - m) / (n + k + 1))


def peso_dia(dia: int, hoy: int) -> float:
    return 0.5 ** (max(0, hoy - dia) / VIDA_MEDIA_DIAS)


@dataclass
class Cifras:
    """Lo medido de una fuente en su ventana. Las noticias de una misma hora comparten mercado (y las 4 monedas se mueven
    juntas): todas juntas son UN caso independiente. casos, direccion, primeras, captadas y medidas cuentan noticias;
    horas, horas_dir y horas_cap son las horas distintas (los casos independientes, para los mínimos); a, f, e, r y t van
    ponderados por antigüedad (para las estimaciones)."""
    casos: int = 0          # noticias con veredicto
    direccion: int = 0      # de ellas, con acierto o fallo
    primeras: int = 0       # que dio antes que nadie y otros confirmaron después
    captadas: int = 0       # captadas hace más de 24 h, con veredicto o sin él (la base de la cobertura del triaje)
    medidas: int = 0        # de esas, con veredicto
    a: float = 0.0          # parte a favor de cada noticia con dirección
    f: float = 0.0
    e: float = 0.0          # lo que habría acertado la tendencia del día (base de mercado)
    r: float = 0.0
    t: float = 0.0
    ultima: int = 0
    tier: str | None = None  # el mejor nivel con que llega (un medio oficial, T0, solo se silencia a mano o por caída)
    horas: set = field(default_factory=set)       # horas con veredicto
    horas_dir: set = field(default_factory=set)   # horas con acierto o fallo
    horas_cap: set = field(default_factory=set)   # horas con noticias captadas: lo que habría si se triara todo

    def add(self, veredicto: str | None, n: int, primeras: int, w: float, ts: int, hora: int | None = None,
            maduras: int | None = None, ac: float | None = None, base: float | None = None) -> None:
        """Un grupo de n noticias de la misma hora y el mismo veredicto (None = sin veredicto: solo cuenta para la
        cobertura). ac y base son sumas de esas n noticias (sin ellas: 1 por acierto, 0 por fallo y base 0,5)."""
        hora = (ts or 0) // H if hora is None else hora
        maduras = (n if veredicto else 0) if maduras is None else (maduras or 0)
        self.captadas += maduras
        self.ultima = max(self.ultima, ts or 0)
        if maduras or veredicto:
            self.horas_cap.add(hora)
        if veredicto is None:
            return
        self.casos += n
        self.medidas += maduras
        self.primeras += primeras or 0
        self.t += w * n
        self.horas.add(hora)
        if veredicto in ("acierto", "fallo"):
            ac = (n if veredicto == "acierto" else 0.0) if ac is None else ac
            self.a += w * ac
            self.f += w * (n - ac)
            self.e += w * (0.5 * n if base is None else base)
            self.direccion += n
            self.horas_dir.add(hora)
        elif veredicto == "ruido":
            self.r += w * n

    @property
    def indep(self) -> int:
        return len(self.horas)

    @property
    def indep_dir(self) -> int:
        return len(self.horas_dir)

    @property
    def dias(self) -> int:
        return len({h // 24 for h in self.horas})

    @property
    def dias_dir(self) -> int:
        return len({h // 24 for h in self.horas_dir})

    def cobertura(self) -> float | None:
        return self.medidas / self.captadas if self.captadas else None

    def falta_triaje(self, need: int) -> bool:
        """True si publicó en horas suficientes pero no se triaron bastantes: la falta de datos no es culpa del medio."""
        return self.indep < need <= len(self.horas_cap)

    def acierto(self) -> tuple[float, float]:
        """Acierto AJUSTADO AL MERCADO (los aciertos por encima de la base del día, centrados en el 50 %) y contraído
        hacia lo neutro; los casos reales se escalan por la parte independiente (horas distintas / noticias)."""
        d = self.a + self.f
        a_aj = min(d, max(0.0, self.a - self.e + 0.5 * d))
        k = min(1.0, self.indep_dir / self.direccion) if self.direccion else 1.0
        return contraido(k * a_aj, k * (d - a_aj))

    def ruido(self, q0: float) -> float:
        k = min(1.0, self.indep / self.casos) if self.casos else 1.0
        return (k * self.r + K_RUIDO * q0) / (k * self.t + K_RUIDO)

    def view(self, q0: float) -> dict:
        m, sd = self.acierto()
        d = self.a + self.f
        cob = self.cobertura()
        return {"casos": self.casos, "independientes": self.indep, "dias": self.dias, "con_direccion": self.direccion,
                "independientes_dir": self.indep_dir, "dias_dir": self.dias_dir, "acierto": round(m, 3),
                "acierto_bruto": round(self.a / d, 3) if d else None, "base_mercado": round(self.e / d, 3) if d else None,
                "acierto_techo90": round(m + Z_CLARO * sd, 3), "ruido": round(self.ruido(q0), 3),
                "ruido_bruto": round(self.r / self.t, 3) if self.t else None, "primeras": self.primeras,
                "captadas": self.captadas, "cobertura": round(cob, 3) if cob is not None else None,
                "ultima": self.ultima or None}


@dataclass
class Tecnico:
    fallos: int              # fallos seguidos (feed_state)
    horas: float             # horas de bot en marcha seguidas fallando (source_health)
    ok: bool | None
    err: str = ""


@dataclass
class Fila:
    """Una fila de source_status."""
    id: str
    estado: str
    desde: int = 0
    motivo: str = ""
    tipo: str = ""           # acierto | ruido | tecnico | pocos_datos | tiempo | sin_datos | mejora | prueba | manual |
                             # descubierta (tiempo: retirada porque en su silencio se confirmó lo malo)
    manual: bool = False
    origen: str = "registro" # registro | descubierta | medio
    previo: str = "activa"   # al restaurar se vuelve aquí
    nombre: str = ""
    spec: dict | None = None
    cifras: dict | None = None

    def protegida(self, ahora: int) -> bool:
        """Lo que cambiaste a mano se respeta MANUAL_DIAS días."""
        return self.manual and ahora - self.desde < MANUAL_DIAS * DAY


def _cob(cif: Cifras) -> str:
    c = cif.cobertura()
    return f"cobertura del triaje {_pct(c)}" if c is not None else "sin noticias maduras"


def _malo(cif: Cifras, q0: float, por_acierto: bool = True) -> tuple[str, str] | None:
    """La condición de silencio, con sus mínimos de casos independientes y de días: (tipo, motivo) o None."""
    m, sd = cif.acierto()
    techo = m + Z_CLARO * sd
    if (por_acierto and cif.indep_dir >= MIN_CASOS and cif.dias_dir >= MIN_DIAS and techo < NEUTRO
            and m <= ACIERTO_MAX):
        return "acierto", (f"Acierta poco: {_pct(m)} ajustado al mercado y contraído en {cif.indep_dir} casos "
                           f"independientes ({cif.direccion} noticias con dirección en {cif.dias_dir} días; techo del "
                           f"90 %: {_pct(techo)}; lo neutro es 50 %)")
    q = cif.ruido(q0)
    if cif.indep >= MIN_CASOS and cif.dias >= MIN_DIAS and q >= RUIDO_MAX:
        return "ruido", (f"Casi todo es ruido: {_pct(q)} contraído en {cif.indep} casos independientes "
                         f"({cif.casos} noticias medidas en {cif.dias} días)")
    return None


def decidir(fila: Fila, cif: Cifras, q0: float, ahora: int, tier: str | None = None, tec: Tecnico | None = None,
            red_caida: bool = False, *, por_acierto: bool = True) -> tuple[str, str, str] | None:
    """Regla de transición: (estado nuevo, tipo, motivo en español) o None si no toca cambiar nada.
    por_acierto=False para los feeds (canales): solo reglas técnicas y de ruido; el acierto se juzga por medio."""
    e = fila.estado
    if e == "retirada":
        return None   # solo a mano
    caida = (tec is not None and not red_caida and tec.ok is False and tec.fallos >= TEC_MIN_FALLOS
             and tec.horas >= TEC_HORAS)
    caida_txt = (f"{tec.fallos} fallos seguidos durante {_n(tec.horas / 24)} días ({tec.err or 'sin respuesta'})"
                 if caida else "")
    oficial = tier in PROTEGIDAS
    dias = (ahora - fila.desde) / DAY if fila.desde else 0.0
    caducada = " (tu cambio a mano ya no la protege: pasaron 30 días)" if fila.manual else ""
    if e in ("activa", "prueba"):
        if caida:   # cuenta siempre, también sobre lo que decidiste a mano
            return "silenciada", "tecnico", f"Caída técnica: {caida_txt}"
        if fila.protegida(ahora):
            return None   # lo decidiste tú hace menos de 30 días
        malo = None if oficial else _malo(cif, q0, por_acierto)
        if malo:
            return "silenciada", malo[0], malo[1] + caducada
        if e == "prueba":
            m, _ = cif.acierto()
            q = cif.ruido(q0)
            if (cif.indep >= MIN_CASOS_ACTIVAR and cif.dias >= MIN_DIAS and q < RUIDO_MAX
                    and (cif.indep_dir < MIN_DIR or m >= NEUTRO)):
                extra = f", acierto {_pct(m)} en {cif.indep_dir} casos" if cif.indep_dir >= MIN_DIR else ""
                return "activa", "prueba", (f"Supera la prueba: {cif.indep} casos independientes en {cif.dias} días "
                                            f"({cif.casos} noticias medidas) sin malos resultados (ruido {_pct(q)}{extra})")
            if (not oficial and fila.desde and dias >= PRUEBA_MAX_DIAS and cif.indep < MIN_CASOS_ACTIVAR
                    and not cif.falta_triaje(MIN_CASOS_ACTIVAR)):
                return "silenciada", "pocos_datos", (
                    f"En {int(dias)} días de prueba solo dio noticias relevantes en {len(cif.horas_cap)} horas distintas "
                    f"({cif.casos} noticias medidas; hacen falta {MIN_CASOS_ACTIVAR} casos independientes): publica poco "
                    "de lo que sigue la estrategia")
        return None
    # silenciada
    if fila.protegida(ahora):
        return None
    if fila.tipo == "tecnico":
        if tec is not None and tec.ok and tec.fallos == 0:
            return fila.previo, "mejora", "Vuelve a responder tras la caída técnica"
        if caida and dias >= RETIRAR_DIAS:
            return "retirada", "tecnico", f"Lleva {int(dias)} días silenciada y sigue caída: {caida_txt}"
        return None   # sin sondeo en esta ejecución (o con la red caída) no se sabe si sigue caída
    m, _ = cif.acierto()
    q = cif.ruido(q0)
    need = MIN_CASOS_RESTAURAR if fila.tipo == "acierto" else MIN_DIR
    acierto_ok = m >= NEUTRO if cif.indep_dir >= need else fila.tipo != "acierto"
    if cif.indep >= MIN_CASOS_RESTAURAR and cif.dias >= MIN_DIAS and q < RUIDO_RESTAURAR and acierto_ok:
        extra = f", acierto {_pct(m)} en {cif.indep_dir} casos" if cif.indep_dir else ""
        return fila.previo, "mejora", (f"Mejora desde que se silenció: {cif.indep} casos independientes en {cif.dias} "
                                       f"días ({cif.casos} noticias), ruido {_pct(q)}{extra}{caducada}")
    if dias < RETIRAR_DIAS:
        return None
    malo = None if oficial else _malo(cif, q0, por_acierto)
    if malo:
        return "retirada", "tiempo", f"Sigue igual tras {int(dias)} días silenciada. {malo[1]}{caducada}"
    # Sin datos para decidir no se retira, salvo a los 90 días si tuvo material y aun así no juntó los casos.
    if (dias >= SILENCIO_MAX_DIAS and fila.tipo != "pocos_datos" and not fila.manual
            and not cif.falta_triaje(MIN_CASOS)):
        return "retirada", "sin_datos", (f"Lleva {int(dias)} días silenciada y en ese tiempo solo dio {cif.indep} casos "
                                         f"independientes (hacen falta {MIN_CASOS}): se retira por falta de datos, no "
                                         "por malos resultados")
    return None


def espera(fila: Fila | None, cif: Cifras | None, ahora: int) -> str | None:
    """Por qué una fuente sigue como está (para la web): protección a mano, sin triaje o sin datos suficientes."""
    if fila is None:
        return None
    cif = cif or Cifras()
    dias = (ahora - fila.desde) / DAY if fila.desde else 0.0
    if fila.protegida(ahora):
        return f"Tu cambio a mano se respeta {max(1, math.ceil(MANUAL_DIAS - dias))} días más"
    if fila.estado == "prueba" and cif.falta_triaje(MIN_CASOS_ACTIVAR):
        return (f"Sin triaje suficiente: publicó en {len(cif.horas_cap)} horas distintas pero solo {cif.indep} tienen "
                f"noticias medidas ({_cob(cif)})")
    if fila.estado == "silenciada" and fila.tipo != "tecnico" and dias >= RETIRAR_DIAS:
        if cif.falta_triaje(MIN_CASOS):
            return (f"Sin triaje suficiente para decidir: {cif.indep} de {MIN_CASOS} casos independientes desde que se "
                    f"silenció ({_cob(cif)}); sigue silenciada")
        return (f"Sin datos suficientes para decidir: {cif.indep} de {MIN_CASOS} casos independientes desde que se "
                "silenció; sigue silenciada")
    return None


def prioridad_triaje(prio: float, estado: str | None, story: str) -> float | None:
    """Prioridad en la cola de la IA local según el estado de la fuente (lo usa la mesa). Activa: la de siempre. Prueba
    y silenciada: 1 de cada MUESTREO_N noticias (fija por su id) entra con bonus de nivel, para que se puedan medir; el
    resto de una prueba va normal y el de una silenciada, al final. Retirada: None (no se tría)."""
    if estado in (None, "activa"):
        return prio
    if estado == "retirada":
        return None
    muestra = int(hashlib.sha1(story.encode()).hexdigest()[:8], 16) % MUESTREO_N == 0
    if muestra:
        return prio + BONO_MUESTRA
    return prio if estado == "prueba" else prio + PRIO_SILENCIADA


# ---------- Descubrimiento (funciones puras) ----------
def enlaces_rss(body: bytes, base: str) -> list[str]:
    """URLs de <link rel="alternate" type="application/rss+xml|atom+xml"> de una portada (sin los de comentarios)."""
    out: list[str] = []
    for tag in LINK_RX.findall(body[:DESCARGA_MAX]):
        attrs = {k.lower(): a or b or c for k, a, b, c in ATTR_RX.findall(tag)}
        if b"alternate" not in attrs.get(b"rel", b"").lower().split():
            continue
        if attrs.get(b"type", b"").lower().strip() not in FEED_TYPES:
            continue
        href = html.unescape(attrs.get(b"href", b"").decode("utf-8", "replace")).strip()
        url = urljoin(base, href) if href else ""
        if url.startswith(("http://", "https://")) and "comment" not in url.lower() and url not in out:
            out.append(url)
    return out


def verificar_feed(raw: bytes, ahora: int, medio: str | None = None) -> tuple[bool, str, dict]:
    """La misma comprobación que el verificador de feeds: RSS/Atom válido, ≥ 3 entradas con título, la última de hace
    menos de 7 días y ≥ 2 en 14 días; además, que la mayoría enlace al propio medio (no es un agregador)."""
    if not _looks_like_feed(raw):
        return False, "No es RSS ni Atom", {}
    items = [i for i in hunters.parse_rss(raw, "verificacion") if i.title.strip()]
    info: dict = {"entradas": len(items), "tipo": "atom" if b"<feed" in raw[:8192].lower() else "rss"}
    if len(items) < FEED_MIN_ENTRADAS:
        return False, f"Solo {len(items)} entradas", info
    fechas = sorted((i.published for i in items if i.published and i.published <= ahora + 600_000), reverse=True)
    if not fechas:
        return False, "Las entradas no tienen fecha", info
    info["ultima_h"] = round(max(0, ahora - fechas[0]) / H, 1)
    info["recientes_14d"] = sum(1 for f in fechas if ahora - f <= 14 * DAY)
    if ahora - fechas[0] > FEED_RECIENTE_DIAS * DAY:
        return False, f"La última entrada es de hace {int((ahora - fechas[0]) / DAY)} días", info
    if info["recientes_14d"] < 2:
        return False, "Menos de 2 entradas en 14 días", info
    if medio:
        sitios = [site(h) for h in (_host(i.url) for i in items) if h]
        sitios = [s for s in sitios if s not in REDIRECT_SITES]
        info["del_medio"] = sum(s == medio for s in sitios)
        if sitios and info["del_medio"] * 2 < len(sitios):
            return False, "El feed enlaza sobre todo a otros medios", info
    t = TITLE_RX.search(raw[:65536])
    if t:
        title = html.unescape(t.group(1).decode("utf-8", "replace")).replace("<![CDATA[", "").replace("]]>", "")
        info["titulo"] = " ".join(title.split())[:120] or None
    return True, f"RSS válido: {len(items)} entradas, la última hace {_n(info['ultima_h'])} h", info


class _SinPresupuesto(Exception):
    """Se acabaron las peticiones de esta hora: se sigue en la ronda siguiente."""


# ---------- Módulo ----------
class FuentesVivas:
    def __init__(self, bus, store, session: aiohttp.ClientSession | None = None, *, veredictos=None, registrar=None,
                 fetch=None, registro_dir: Path = config.FEEDS_DIR, auto: bool | None = None,
                 descubrir: bool | None = None):
        self.bus, self.store, self.session = bus, store, session
        self.veredictos = veredictos            # aprendizaje (ver docstring); None = solo el triaje
        self.registrar = registrar              # evolucion.evolution_log (ver docstring)
        self.fetch = fetch or self._fetch       # async (url) -> (estado HTTP, url final, cuerpo); las pruebas lo cambian
        self.registro_dir = Path(registro_dir)
        self.auto = AUTO if auto is None else auto
        self.descubrir = DESCUBRIR if descubrir is None else descubrir
        self.poller = None
        self.filas: dict[str, Fila] = {}
        self.salud: dict[str, float] = {}       # id → horas seguidas fallando
        self.cifras: dict[str, Cifras] = {}
        self.q0 = NEUTRO                        # ruido medio de todas las fuentes
        self.sugerencias: dict[str, tuple[str, str, str]] = {}   # con auto apagado (o por el tope): lo que se haría
        self._tareas: set[asyncio.Task] = set()  # escrituras del registro lanzadas desde acciones a mano
        self.propios: set[str] = set(HUNTER_SITES)   # medios con feed propio (no son candidatos)
        self.revision = {"ultima": None, "s": None, "medidas": 0, "cambios": 0, "nota": None}
        self.presupuesto = PETICIONES_HORA
        self._buf: list[tuple] = []
        self._robots: dict[str, tuple[float, RobotFileParser | None]] = {}
        self._ultima_peticion = 0.0
        self._ultima_salud: int | None = None
        self._scan_ts = 0
        self._last_pub: str | None = None
        self._ext_errors = 0
        self._closed = False
        self.clock = time.monotonic
        self.sleep = asyncio.sleep
        self._crear_tablas()
        self._cargar()

    # ----- base de datos
    def _crear_tablas(self) -> None:
        self.store.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS source_status(id TEXT PRIMARY KEY, estado TEXT NOT NULL, desde INTEGER NOT NULL,
              motivo TEXT, tipo TEXT, manual INTEGER NOT NULL DEFAULT 0, origen TEXT, previo TEXT, nombre TEXT, spec TEXT,
              cifras TEXT);
            CREATE TABLE IF NOT EXISTS source_status_log(ts INTEGER NOT NULL, id TEXT NOT NULL, antes TEXT, despues TEXT,
              tipo TEXT, motivo TEXT, cifras TEXT, auto INTEGER, por TEXT);
            CREATE INDEX IF NOT EXISTS source_status_log_id ON source_status_log(id, ts);
            CREATE TABLE IF NOT EXISTS source_health(id TEXT PRIMARY KEY, horas REAL NOT NULL, desde INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS fv_noticias(story TEXT PRIMARY KEY, ts INTEGER NOT NULL, feed TEXT NOT NULL,
              medio TEXT, nombre TEXT, host TEXT, region TEXT, lang TEXT, tier TEXT, lat_s REAL, veredicto TEXT,
              confirmada INTEGER NOT NULL DEFAULT 0, medir_en INTEGER, triada INTEGER, ac REAL, base REAL);
            CREATE INDEX IF NOT EXISTS fv_noticias_ts ON fv_noticias(ts);
            CREATE INDEX IF NOT EXISTS fv_noticias_medio ON fv_noticias(medio, ts);
            CREATE INDEX IF NOT EXISTS fv_noticias_medir ON fv_noticias(medir_en) WHERE medir_en IS NOT NULL;
            CREATE TABLE IF NOT EXISTS fv_candidatos(medio TEXT PRIMARY KEY, nombre TEXT, region TEXT, lang TEXT,
              host TEXT, estado TEXT NOT NULL, visto INTEGER, intento INTEGER, intentos INTEGER NOT NULL DEFAULT 0,
              rss TEXT, feed TEXT, nota TEXT, cifras TEXT, info TEXT);
            """
        )
        # Columnas nuevas en una base de antes (triada: llegó al triaje; ac y base: acierto ajustado al mercado).
        for tabla, cols in (("fv_noticias", ("triada INTEGER", "ac REAL", "base REAL")), ("fv_candidatos", ("info TEXT",))):
            have = {r[1] for r in self.store.db.execute(f"PRAGMA table_info({tabla})").fetchall()}
            for col in cols:
                if col.split()[0] not in have:
                    self.store.db.execute(f"ALTER TABLE {tabla} ADD COLUMN {col}")
        self.store.db.commit()

    def _cargar(self) -> None:
        db = self.store.db
        for r in db.execute("SELECT id, estado, desde, motivo, tipo, manual, origen, previo, nombre, spec, cifras "
                            "FROM source_status").fetchall():
            if r[1] not in ESTADOS:
                continue
            self.filas[r[0]] = Fila(r[0], r[1], r[2] or 0, r[3] or "", r[4] or "", bool(r[5]), r[6] or "registro",
                                    r[7] if r[7] in ("activa", "prueba") else "activa", r[8] or "",
                                    _loads(r[9]), _loads(r[10]))
        self.salud = {i: float(h) for i, h in db.execute("SELECT id, horas FROM source_health").fetchall()}
        row = db.execute(f"SELECT MAX(ts) FROM fv_noticias WHERE feed IN ({','.join('?' * len(RELAYS))})", RELAYS).fetchone()
        self._scan_ts = row[0] or now_ms() - 6 * H

    @contextmanager
    def _escritor(self):
        """Conexión propia para escribir desde un hilo (transacciones cortas: el bucle no espera)."""
        if str(self.store.path) == ":memory:":
            yield self.store.db
            return
        db = sqlite3.connect(self.store.path, timeout=30)
        try:
            yield db
        finally:
            db.close()

    # ----- enganche con los feeds
    def attach(self, poller) -> None:
        """Conecta con el FeedPoller (mejor antes de poller.run()): estados guardados, feeds descubiertos y el gancho."""
        self.poller = poller
        poller.on_item = self.observar
        for f in self.filas.values():
            if f.origen == "medio":
                poller.set_outlet_estado(f.id.removeprefix(MEDIO), f.estado)
            elif f.id in poller.states:
                poller.set_estado(f.id, f.estado)
            elif f.spec:   # descubierta que no llegó al JSON del registro (o se borró de él): sale de la base
                try:
                    poller.add_feed(FeedSpec(**f.spec), f.estado)
                except (TypeError, ValueError) as e:
                    log.warning("Feed descubierto %s con datos rotos: %s", f.id, e)
        for st in poller.states.values():
            if st.host and not st.gnews and site(st.host) not in REDIRECT_SITES:
                self.propios.add(site(st.host))

    def observar(self, st, it, res: str) -> None:
        """Gancho de FeedPoller.on_item: apunta cada noticia captada con su feed, su medio, su hora y su latencia."""
        if res != "captured":
            return
        now = now_ms()
        medio = (it.outlet or "").strip().lower() or None
        if medio == st.spec.id.lower():
            medio = None   # el feed no sabe qué medio la firma
        host = _host(it.url or "")
        if not host or host == GOOGLE_NEWS_HOST or site(host) in REDIRECT_SITES:
            host = None
        lat = (now - it.published) / 1000 if it.published else None
        self._buf.append((story_id(it.source, it.uid), now, st.spec.id, medio, it.via, host, it.region, it.lang,
                          it.tier or st.spec.tier, lat, None, 0, now + MEDIR_H[0] * H))
        if len(self._buf) > BUF_MAX:   # la base no responde: se pierden las más viejas, no la memoria
            del self._buf[:len(self._buf) - BUF_MAX]

    def volcar(self) -> int:
        """Guarda lo apuntado por observar (en el bucle: son pocas filas cada 30 s)."""
        rows, self._buf = self._buf, []
        if not rows:
            return 0
        try:
            self.store.db.executemany(f"INSERT OR IGNORE INTO fv_noticias ({FV_COLS}) VALUES ({','.join('?' * 13)})",
                                      rows)
            self.store.db.commit()
        except sqlite3.Error as e:
            log.warning("No se pudieron guardar %d noticias medidas: %s", len(rows), e)
            self._buf = (rows + self._buf)[-BUF_MAX:]
            return 0
        return len(rows)

    def _escanear_relays(self, desde: int, propios: frozenset = frozenset(HUNTER_SITES)) -> tuple[list[tuple], int]:
        """Noticias captadas de relays (Tree News): su enlace dice el medio (también si tiene feed propio: el medio se
        juzga con todos sus canales). Sin los que leen los cazadores directos (propios). En un hilo."""
        rows, last = [], desde
        with self.store.reader() as db:
            for story, ts, body in db.execute("SELECT story, ts, body FROM events WHERE kind='captured' AND ts>? "
                                              "ORDER BY ts LIMIT 20000", (desde,)).fetchall():
                last = max(last, ts)
                try:
                    p = json.loads(body).get("patch") or {}
                except (ValueError, AttributeError):
                    continue
                if p.get("srcId") not in RELAYS:
                    continue
                host = _host(str(p.get("url") or ""))
                medio = site(host) if host else None
                if not medio or medio in propios or medio in NO_RSS or medio in REDIRECT_SITES:
                    continue
                lat = p.get("lat")
                rows.append((story, ts, p["srcId"], medio, p.get("via"), host, p.get("region"), p.get("lang"),
                             p.get("tier"), lat / 1000 if isinstance(lat, (int, float)) else None, None, 0,
                             ts + MEDIR_H[0] * H))
        return rows, last

    async def escanear_relays(self) -> int:
        rows, self._scan_ts = await asyncio.to_thread(self._escanear_relays, self._scan_ts, frozenset(HUNTER_SITES))
        self._buf.extend(rows)
        return len(rows)

    # ----- medida (en un hilo)
    def _externos(self, stories: list[str]) -> dict:
        if not self.veredictos:
            return {}
        try:
            out = self.veredictos(stories)
            return out if isinstance(out, dict) else {}
        except Exception:
            self._ext_errors += 1
            if self._ext_errors in (1, 100) or self._ext_errors % 1000 == 0:
                log.exception("El aprendizaje no dio veredictos (%d veces): se usa solo el triaje", self._ext_errors)
            return {}

    @staticmethod
    def _de_aprendizaje(db, ids: list[str]) -> dict[str, tuple[str, float | None, float | None]]:
        """Veredicto con las medidas del aprendizaje (news_outcomes, fila del medio que la dio primero; una noticia
        global cuenta una vez), AJUSTADO AL MERCADO como en aprendizaje.py: cada medida a 1 h y 4 h por moneda que pasa
        del umbral es a favor o en contra, y su base es lo que habría acertado la tendencia de ese día (mercado_base:
        de las veces que esa moneda se movió más del umbral, qué parte fue en la dirección prevista; sin base, 0,5).
        Devuelve story → (veredicto, ac, base): ac = parte de sus medidas a favor y base = la de la tendencia (las dos
        de 0 a 1; None sin dirección). Archivada → ruido; sin dirección prevista o todo dentro del umbral → util; el
        veredicto es «acierto» si hay más medidas a favor que en contra (las reglas usan ac − base). Sin medir → nada."""
        marks = ",".join("?" * len(ids))
        con_base = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mercado_base'").fetchone()
        if con_base:
            sql = (f"SELECT o.story, o.archivada, o.dir, o.r60, o.r240, o.estado, mb.s60, mb.b60, mb.s240, mb.b240 "
                   f"FROM news_outcomes o LEFT JOIN mercado_base mb ON mb.coin = o.coin AND mb.dia = o.t0 / {DAY} "
                   f"WHERE o.rol = 'primera' AND o.story IN ({marks})")
        else:
            sql = (f"SELECT story, archivada, dir, r60, r240, estado, NULL, NULL, NULL, NULL FROM news_outcomes "
                   f"WHERE rol = 'primera' AND story IN ({marks})")
        by: dict[str, list] = defaultdict(list)
        for r in db.execute(sql, ids).fetchall():
            by[r[0]].append(r[1:])
        out = {}
        for story, rs in by.items():
            if any(a == 1 for a, *_ in rs):
                out[story] = ("ruido", None, None)
                continue
            if all(not d for _, d, *_ in rs):
                out[story] = ("util", None, None)
                continue
            s = f = medidas = 0
            e = 0.0
            for _, d, r60, r240, est, s60, b60, s240, b240 in rs:
                if not d or est == "sin_datos":
                    continue
                for r, sb, bb in ((r60, s60, b60), (r240, s240, b240)):
                    if r is None:
                        continue
                    medidas += 1
                    x = d * r
                    if abs(x) < UMBRAL_PCT:
                        continue   # dentro del umbral: ruido de mercado
                    ok_base = sb is not None and bb is not None and sb + bb > 0
                    e += ((sb if d > 0 else bb) / (sb + bb)) if ok_base else 0.5
                    if x > 0:
                        s += 1
                    else:
                        f += 1
            if not medidas:
                continue   # aún sin medir: decide el triaje
            if not s + f:
                out[story] = ("util", None, None)
                continue
            ac, base = s / (s + f), e / (s + f)
            out[story] = ("acierto" if s > f or (s == f and ac >= base) else "fallo", ac, base)
        return out

    @staticmethod
    def _siguiente(ts: int, ahora: int) -> int | None:
        for h in MEDIR_H[1:]:
            if ts + h * H > ahora:
                return ts + h * H
        return None   # cerrada

    def _medir(self, ahora: int) -> int:
        """Pone veredicto, si llegó al triaje y confirmaciones a las noticias que tocan. En un hilo, con conexiones
        propias."""
        updates = []
        with self.store.reader() as db:
            pend = db.execute("SELECT story, ts FROM fv_noticias WHERE medir_en IS NOT NULL AND medir_en<=? "
                              "ORDER BY medir_en LIMIT ?", (ahora, MEDIR_MAX)).fetchall()
            apr = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='news_outcomes'").fetchone()
            for i in range(0, len(pend), LOTE):
                chunk = pend[i:i + LOTE]
                ids = [s for s, _ in chunk]
                low, tri, conf = set(), set(), Counter()
                for story, kind in db.execute(
                        f"SELECT story, kind FROM events WHERE story IN ({','.join('?' * len(ids))}) "
                        "AND kind IN ('triaged', 'lowprio', 'corroborated')", ids).fetchall():
                    if kind == "corroborated":
                        conf[story] += 1
                    else:
                        (low if kind == "lowprio" else tri).add(story)
                ext = self._externos(ids)
                try:
                    med = self._de_aprendizaje(db, ids) if apr else {}
                except sqlite3.Error as e:   # tabla a medio crear o con otro formato: se sigue con el triaje
                    log.warning("No pude leer las medidas del aprendizaje: %s", e)
                    med, apr = {}, None
                for story, ts in chunk:
                    v, ac, base = ext.get(story), None, None
                    if v in ("acierto", "fallo"):
                        ac = 1.0 if v == "acierto" else 0.0
                    if v not in VEREDICTOS:
                        v, ac, base = med.get(story, (None, None, None))
                    if v not in VEREDICTOS:
                        v = "ruido" if story in low else "util" if story in tri else None
                    triada = int(story in low or story in tri or story in med)   # news_outcomes solo tiene triadas
                    updates.append((v, conf[story], self._siguiente(ts, ahora), triada, ac, base, story))
        with self._escritor() as db:
            for i in range(0, len(updates), LOTE):
                db.executemany("UPDATE fv_noticias SET veredicto=?, confirmada=?, medir_en=?, triada=?, ac=?, base=? "
                               "WHERE story=?", updates[i:i + LOTE])
                db.commit()
        return len(updates)

    def _calcular(self, ahora: int, desde: dict[str, int], propios: frozenset):
        """Cifras por feed y por medio (desde el día siguiente a su último cambio) y de los candidatos (últimos 30 días,
        sin feed propio), agrupadas por HORA: las noticias de una misma hora son un solo caso independiente. Cuenta
        también las captadas sin veredicto (cobertura del triaje). Devuelve (cifras, ruido medio, candidatos). En un
        hilo."""
        hoy = ahora // DAY
        out: dict[str, Cifras] = defaultdict(Cifras)
        cand: dict[str, Cifras] = defaultdict(Cifras)
        r_tot = t_tot = 0.0
        q = ("SELECT {col}, ts / 3600000, veredicto, COUNT(*), SUM(confirmada > 0), MAX(ts), SUM(ts <= ?), "
             "SUM(CASE WHEN veredicto IN ('acierto', 'fallo') THEN COALESCE(ac, veredicto = 'acierto') END), "
             "SUM(CASE WHEN veredicto IN ('acierto', 'fallo') THEN COALESCE(base, 0.5) END), MIN(tier) "
             "FROM fv_noticias WHERE ts >= ? {extra} GROUP BY 1, 2, 3")
        args = (ahora - MEDIR_H[1] * H, ahora - VENTANA_DIAS * DAY)   # maduras: captadas hace más de 24 h
        with self.store.reader() as db:
            for feed, hora, v, n, prim, ult, mad, ac, base, _ in db.execute(q.format(col="feed", extra=""),
                                                                             args).fetchall():
                w = peso_dia(hora // 24, hoy)
                if v is not None:
                    t_tot += w * n
                    r_tot += w * n if v == "ruido" else 0
                d0 = desde.get(feed)
                if d0 is None or hora // 24 > d0 // DAY:
                    out[feed].add(v, n, prim, w, ult, hora, mad, ac, base)
            for medio, hora, v, n, prim, ult, mad, ac, base, tier in db.execute(
                    q.format(col="medio", extra="AND medio IS NOT NULL"), args).fetchall():
                key, w = MEDIO + medio, peso_dia(hora // 24, hoy)
                c = out[key]
                if tier and (c.tier is None or tier < c.tier):
                    c.tier = tier
                d0 = desde.get(key)
                if d0 is None or hora // 24 > d0 // DAY:
                    c.add(v, n, prim, w, ult, hora, mad, ac, base)
                if (d0 is None and medio not in propios and hora // 24 >= hoy - CAND_DIAS and "." in medio
                        and medio not in NO_RSS):
                    cand[medio].add(v, n, prim, w, ult, hora, mad, ac, base)
        return dict(out), (r_tot / t_tot if t_tot else NEUTRO), dict(cand)

    def _meta(self, medios: list[str], desde_ms: int) -> dict[str, dict]:
        """Región, idioma, nombre y host más frecuentes de cada candidato. En un hilo."""
        out = {}
        with self.store.reader() as db:
            for medio in medios:
                rows = db.execute("SELECT region, lang, nombre, host, feed, COUNT(*) FROM fv_noticias WHERE medio=? "
                                  "AND ts>=? GROUP BY 1, 2, 3, 4, 5", (medio, desde_ms)).fetchall()
                c = {k: Counter() for k in ("region", "lang", "nombre", "host", "feed")}
                for region, lang, nombre, host, feed, n in rows:
                    for k, v in zip(c, (region, lang, nombre, host, feed)):
                        if v:
                            c[k][v] += n
                out[medio] = {k: (v.most_common(1)[0][0] if v else None) for k, v in c.items()}
        return out

    def _podar(self, antes: int) -> int:
        total = 0
        with self._escritor() as db:
            while True:
                n = db.execute("DELETE FROM fv_noticias WHERE rowid IN (SELECT rowid FROM fv_noticias WHERE ts < ? "
                               "LIMIT 2000)", (antes,)).rowcount
                db.commit()
                total += n
                if n < 2000:
                    return total

    # ----- salud técnica (en el bucle: es poco trabajo)
    def _salud(self, ahora: int) -> bool:
        """Suma las horas seguidas que falla cada feed (solo cuenta el tiempo con el bot en marcha, entre revisiones).
        Devuelve True si parece caída nuestra red (falla más del 30 % de lo sondeado): entonces no se cuenta nada."""
        if self.poller is None:
            return False
        polled = [st for st in self.poller.states.values() if st.estado != "retirada" and st.ok is not None]
        failing = [st for st in polled if st.ok is False]
        inc = 0.0 if self._ultima_salud is None else min((ahora - self._ultima_salud) / H, 2.0)
        self._ultima_salud = ahora
        if polled and len(failing) > TEC_MAX_RED * len(polled):
            log.warning("Fallan %d de %d feeds: parece un problema de nuestra red, no se cuenta como caída de nadie",
                        len(failing), len(polled))
            return True
        rows, gone = [], []
        for st in polled:
            fid = st.spec.id
            if st.ok is False and st.fails > 0:
                self.salud[fid] = self.salud.get(fid, 0.0) + inc
                rows.append((fid, self.salud[fid], ahora))
            elif fid in self.salud:
                del self.salud[fid]
                gone.append((fid,))
        db = self.store.db
        db.executemany("INSERT OR REPLACE INTO source_health VALUES (?,?,?)", rows)
        db.executemany("DELETE FROM source_health WHERE id=?", gone)
        db.commit()
        return False

    # ----- cambios de estado
    def _nombre(self, clave: str) -> str:
        f = self.filas.get(clave)
        if f and f.nombre:
            return f.nombre
        st = self.poller.states.get(clave) if self.poller else None
        return st.spec.name if st else clave.removeprefix(MEDIO)

    def _cambiar(self, clave: str, nuevo: str, tipo: str, motivo: str, cifras: dict | None = None, *, auto: bool,
                 por: str, origen: str | None = None, spec: dict | None = None, nombre: str | None = None) -> dict:
        ahora = now_ms()
        es_medio = clave.startswith(MEDIO)
        fila = self.filas.get(clave)
        if fila is None:   # sin fila: el estado que tenga en el poller (un feed del registro, «activa»)
            st = self.poller.states.get(clave) if self.poller is not None and not es_medio else None
            fila = Fila(clave, st.estado if st else "activa", 0, origen=origen or ("medio" if es_medio else "registro"))
            antes = None if spec else fila.estado   # con spec es un alta (descubierta)
        else:
            antes = fila.estado
        previo = antes if antes in ("activa", "prueba") and nuevo in ("silenciada", "retirada") else fila.previo
        nueva = replace(fila, estado=nuevo, desde=ahora, motivo=motivo, tipo=tipo, manual=not auto, previo=previo,
                        nombre=nombre or fila.nombre or self._nombre(clave), spec=spec or fila.spec, cifras=cifras)
        db = self.store.db
        db.execute("INSERT OR REPLACE INTO source_status VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (clave, nuevo, ahora, motivo, tipo, int(not auto), nueva.origen, nueva.previo, nueva.nombre,
                    _dumps(nueva.spec), _dumps(cifras)))
        db.execute("INSERT INTO source_status_log VALUES (?,?,?,?,?,?,?,?,?)",
                   (ahora, clave, antes, nuevo, tipo, motivo, _dumps(cifras), int(auto), por))
        db.commit()
        self.filas[clave] = nueva
        self.sugerencias.pop(clave, None)
        if self.poller is not None:
            if es_medio:   # vale para todos sus canales: su feed propio, Google News y los relays
                self.poller.set_outlet_estado(clave.removeprefix(MEDIO), nuevo)
            else:
                self.poller.set_estado(clave, nuevo)
        accion = _accion(antes, nuevo)
        entrada = {"ts": ahora, "area": "fuentes", "accion": accion, "id": clave, "nombre": nueva.nombre, "antes": antes,
                   "despues": nuevo, "tipo": tipo, "motivo": motivo, "cifras": cifras, "auto": auto, "por": por,
                   "reversible": True, "deshacer": {"accion": _deshacer(antes, nuevo), "id": clave}}
        log.info("Fuente %s (%s): %s → %s · %s", clave, nueva.nombre, antes, nuevo, motivo)
        if self.registrar is not None:
            try:
                self.registrar(entrada)
            except Exception:
                log.exception("No se pudo apuntar el cambio de %s en la evolución", clave)
        return entrada

    def _cifras_de(self, clave: str, tec: Tecnico | None = None) -> dict:
        out = (self.cifras.get(clave) or Cifras()).view(self.q0)
        out["ruido_medio"] = round(self.q0, 3)
        if tec is not None:
            out["tecnico"] = {"fallos": tec.fallos, "horas_fallando": round(tec.horas, 1), "err": tec.err or None}
        return out

    def _tecnico(self, st) -> Tecnico:
        return Tecnico(st.fails, self.salud.get(st.spec.id, 0.0), st.ok, st.err)

    def _bajadas_24h(self, ahora: int) -> int:
        """Silencios y retiradas automáticas (no técnicas) de las últimas 24 h."""
        return self.store.db.execute(
            "SELECT COUNT(*) FROM source_status_log WHERE ts > ? AND auto = 1 AND despues IN ('silenciada', 'retirada') "
            "AND tipo != 'tecnico'", (ahora - DAY,)).fetchone()[0]

    def _gravedad(self, tipo: str, cif: Cifras) -> float:
        """Orden de los silencios cuando hay tope: primero lo más claro (techo de acierto más bajo, más ruido)."""
        if tipo == "acierto":
            m, sd = cif.acierto()
            return m + Z_CLARO * sd
        if tipo == "ruido":
            return 1 - cif.ruido(self.q0)
        return 1.0

    def aplicar_reglas(self, ahora: int, red_caida: bool = False) -> list[dict]:
        """Recorre feeds (canales: técnica y ruido) y medios (acierto y ruido) con las reglas. Con auto apagado solo deja
        sugerencias; con auto, como mucho MAX_BAJADAS_REVISION silencios o retiradas no técnicas por revisión y
        MAX_BAJADAS_DIA en 24 h (lo que pase del tope queda como sugerencia, lo más claro primero)."""
        cambios, bajadas = [], []
        sts = self.poller.states if self.poller is not None else {}
        claves = set(sts) | {k for k in self.filas if k.startswith(MEDIO)} | {k for k in self.cifras if k.startswith(MEDIO)}
        for clave in sorted(claves):
            st = sts.get(clave)
            es_medio = clave.startswith(MEDIO)
            fila = self.filas.get(clave) or Fila(clave, st.estado if st else "activa",
                                                 origen="medio" if es_medio else "registro")
            cif = self.cifras.get(clave) or Cifras()
            tec = self._tecnico(st) if st is not None else None
            d = decidir(fila, cif, self.q0, ahora, st.spec.tier if st else cif.tier, tec, red_caida, por_acierto=es_medio)
            if d is None:
                self.sugerencias.pop(clave, None)
                continue
            if not self.auto:
                self.sugerencias[clave] = d
                continue
            if d[0] in ("silenciada", "retirada") and d[1] != "tecnico":
                bajadas.append((self._gravedad(d[1], cif), clave, d, tec))
                continue
            cambios += self._aplicar(clave, d, tec)
        cupo = max(0, min(MAX_BAJADAS_REVISION, MAX_BAJADAS_DIA - self._bajadas_24h(ahora))) if bajadas else 0
        for i, (_, clave, d, tec) in enumerate(sorted(bajadas, key=lambda b: (b[0], b[1]))):
            if i < cupo:
                cambios += self._aplicar(clave, d, tec)
            else:
                self.sugerencias[clave] = (d[0], d[1], f"{d[2]} · Queda como sugerencia: como mucho {MAX_BAJADAS_REVISION} "
                                           f"silencios automáticos por revisión y {MAX_BAJADAS_DIA} al día")
        return cambios

    def _aplicar(self, clave: str, d: tuple[str, str, str], tec: Tecnico | None) -> list[dict]:
        nuevo, tipo, motivo = d
        try:
            return [self._cambiar(clave, nuevo, tipo, motivo, self._cifras_de(clave, tec), auto=True, por="automático")]
        except sqlite3.Error:
            log.exception("No se pudo cambiar el estado de %s", clave)
            return []

    # ----- acciones a mano (web y deshacer)
    def _existe(self, clave: str) -> bool:
        """¿Fuente conocida? Un feed del registro, una fila guardada o, con «medio:x», un medio ya visto: con cifras, con
        estado en los feeds, con feed propio, con noticias captadas, en el catálogo del aprendizaje o candidato. Otra
        cadena no crea una fuente inventada (su cambio quedaría para siempre en el historial)."""
        if clave in self.filas:
            return True
        if not clave.startswith(MEDIO):
            return self.poller is not None and clave in self.poller.states
        medio = clave[len(MEDIO):]
        if clave in self.cifras:
            return True
        if not HOST_RX.fullmatch(medio):
            return False
        if medio in self.propios or (self.poller is not None and medio in self.poller.outlet_estados):
            return True
        for sql in ("SELECT 1 FROM fv_noticias WHERE medio = ? LIMIT 1", "SELECT 1 FROM fv_candidatos WHERE medio = ?",
                    "SELECT 1 FROM medios_info WHERE outlet = ?"):
            try:
                if self.store.db.execute(sql, (medio,)).fetchone():
                    return True
            except sqlite3.Error:   # sin la tabla del aprendizaje
                continue
        return False

    def _manual(self, clave: str, accion: str, motivo: str | None, por: str) -> dict:
        if not isinstance(clave, str) or not clave or len(clave) > 300 or not self._existe(clave):
            return {"ok": False, "error": "No conozco esa fuente"}
        nuevo, permitidos, base = MANUAL[accion]
        fila = self.filas.get(clave)
        actual = fila.estado if fila else (
            self.poller.states[clave].estado if self.poller and clave in self.poller.states else "activa")
        if actual not in permitidos:
            return {"ok": False, "id": clave, "estado": actual, "error": f"No se puede: está {actual}"}
        nuevo = nuevo or (fila.previo if fila else "activa")
        texto = " ".join(str(motivo or "").split())[:200]
        e = self._cambiar(clave, nuevo, "manual", f"{base}: {texto}" if texto else base, self._cifras_de(clave),
                          auto=False, por=por)
        self.publicar()
        return {"ok": True, "id": clave, "antes": e["antes"], "estado": nuevo}

    def restaurar(self, clave: str, motivo: str | None = None, por: str = "tú, desde la web") -> dict:
        """Devuelve una silenciada o retirada al estado que tenía antes de silenciarse (activa o prueba)."""
        return self._manual(clave, "restaurar", motivo, por)

    def silenciar(self, clave: str, motivo: str | None = None, por: str = "tú, desde la web") -> dict:
        return self._manual(clave, "silenciar", motivo, por)

    def retirar(self, clave: str, motivo: str | None = None, por: str = "tú, desde la web") -> dict:
        return self._manual(clave, "retirar", motivo, por)

    def activar(self, clave: str, motivo: str | None = None, por: str = "tú, desde la web") -> dict:
        return self._manual(clave, "activar", motivo, por)

    def a_prueba(self, clave: str, motivo: str | None = None, por: str = "tú, desde la web") -> dict:
        return self._manual(clave, "a_prueba", motivo, por)

    def accion(self, msg: dict) -> dict:
        """Orden de la web o de deshacer: {"type": "fuente", "accion": "restaurar|silenciar|retirar|activar|a_prueba|
        anadir", "id": "...", "motivo": "..."}. «anadir» da de alta un candidato verificado (id = su dominio)."""
        accion, clave = msg.get("accion"), msg.get("id")
        if accion in MANUAL:
            return self._manual(clave, accion, msg.get("motivo"), "tú, desde la web")
        if accion == "anadir":
            return self.anadir(clave, msg.get("motivo"))
        return {"ok": False, "error": "Acción desconocida"}

    # ----- descubrimiento
    async def _fetch(self, url: str) -> tuple[int, str, bytes]:
        """GET honesto: identificador del bot, sin credenciales; solo el principio de respuestas enormes."""
        if self.session is None:
            raise RuntimeError("Sin sesión HTTP")
        headers = {"User-Agent": config.USER_AGENT, "Accept": ACCEPT}
        async with self.session.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True, max_redirects=5) as r:
            body = bytearray()
            async for chunk in r.content.iter_chunked(65536):
                body += chunk
                if len(body) >= DESCARGA_MAX:
                    break
            return r.status, str(r.url), bytes(body)

    async def _pedir(self, url: str) -> tuple[int, str, bytes]:
        if self.presupuesto <= 0:
            raise _SinPresupuesto
        self.presupuesto -= 1
        gap = PAUSA_S - (self.clock() - self._ultima_peticion)
        if self._ultima_peticion and gap > 0:
            await self.sleep(gap)
        self._ultima_peticion = self.clock()
        return await self.fetch(url)

    async def _robots_de(self, url: str) -> RobotFileParser | None:
        """robots.txt del sitio (caché de un día). None si ahora no se puede leer (5xx, 429 o red): no se sigue."""
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        hit = self._robots.get(origin)
        if hit and time.time() - hit[0] < 86400:
            return hit[1]
        try:
            status, _, body = await self._pedir(origin + "/robots.txt")
        except _SinPresupuesto:
            raise
        except Exception as e:
            log.info("robots.txt de %s ilegible: %s", origin, e)
            return None
        rp = RobotFileParser()
        if status == 200:
            rp.parse(body.decode("utf-8", "replace").splitlines())
        elif status in (401, 403):
            rp.disallow_all = True    # como la biblioteca estándar: acceso vetado
        elif 400 <= status < 500 and status != 429:
            rp.parse([])              # sin robots.txt: se puede leer
        else:
            return None
        self._robots[origin] = (time.time(), rp)
        return rp

    async def _buscar(self, medio: str, host: str | None, ahora: int) -> tuple[str, str, str | None, dict | None]:
        """Busca y verifica el RSS de un medio. Devuelve (estado, nota, url del feed, info)."""
        base = f"https://{host or medio}"
        rp = await self._robots_de(base)
        if rp is None:
            return "error", "No se pudo leer robots.txt", None, None
        if not rp.can_fetch(config.USER_AGENT, base + "/"):
            return "bloqueado", "robots.txt no deja leer la portada", None, None
        try:
            status, final, body = await self._pedir(base + "/")
        except _SinPresupuesto:
            raise
        except Exception as e:
            return "error", f"La portada no responde ({type(e).__name__})", None, None
        if status in BLOQUEO:
            return "bloqueado", f"La portada responde HTTP {status}", None, None
        if status >= 400:
            return "error", f"La portada responde HTTP {status}", None, None
        urls = enlaces_rss(body, final) + [urljoin(final, r) for r in RUTAS_RSS]
        urls = [u for i, u in enumerate(urls) if u not in urls[:i] and _host(u) != GOOGLE_NEWS_HOST]
        nota = "Ni la portada ni las rutas típicas dan un RSS"
        for url in urls[:MAX_PRUEBAS]:
            if self.poller is not None and self.poller.has_url(url):
                return "en_registro", "Su RSS ya está en el registro", url, None
            rp2 = rp if url.startswith(base) else await self._robots_de(url)
            if rp2 is None or not rp2.can_fetch(config.USER_AGENT, url):
                continue
            try:
                status, final2, raw = await self._pedir(url)
            except _SinPresupuesto:
                raise
            except Exception:
                continue
            if status in BLOQUEO:
                return "bloqueado", f"{url} responde HTTP {status}", None, None
            if status != 200:
                continue
            ok, why, info = await asyncio.to_thread(verificar_feed, raw, ahora, medio)
            if ok:
                if self.poller is not None and self.poller.has_url(final2):
                    return "en_registro", "Su RSS ya está en el registro", final2, None
                return "anadido", why, final2, info
            nota = f"{url}: {why}"
        return "sin_rss", nota, None, None

    def _aptos(self, cand: dict[str, Cifras]) -> list[tuple[str, Cifras]]:
        out = []
        for medio, c in cand.items():
            if medio in self.propios or MEDIO + medio in self.filas or medio in NO_RSS:
                continue
            m, _ = c.acierto()
            if c.indep < CAND_MIN_CASOS or c.ruido(self.q0) >= RUIDO_MAX:
                continue
            if c.indep_dir >= MIN_DIR and m < NEUTRO:
                continue   # da noticias, pero las que se pueden medir fallan
            if c.primeras >= CAND_MIN_PRIMERAS or (c.indep_dir >= MIN_DIR and m >= CAND_ACIERTO_MIN):
                out.append((medio, c))
        return sorted(out, key=lambda x: (-x[1].primeras, -x[1].indep, x[0]))

    @staticmethod
    def _motivo_alta(v: dict, meta: dict) -> str:
        """Por qué se da de alta un medio nuevo, con las cifras de Cifras.view()."""
        via = meta.get("feed") or "un agregador"
        extra = (f", acierto {_pct(v['acierto'])} en {v['independientes_dir']} casos"
                 if (v.get("independientes_dir") or 0) >= MIN_DIR else "")
        return (f"Medio nuevo visto en {via}: {v.get('casos', 0)} noticias medidas ({v.get('independientes', 0)} casos "
                f"independientes) en {CAND_DIAS} días, {v.get('primeras', 0)} antes que nadie{extra}, ruido "
                f"{_pct(v.get('ruido') or 0)}")

    async def descubrir_medios(self, ahora: int, cand: dict[str, Cifras]) -> list[dict]:
        """Apunta los candidatos y busca el RSS de los que tocan, con el presupuesto de peticiones de esta hora. Con el
        automático apagado se busca y se verifica igual, pero el RSS queda «verificado» (pendiente de que lo añadas tú
        con la acción «anadir»): no se da de alta nada ni se escribe el registro."""
        db = self.store.db
        aptos = self._aptos(cand)
        known = {r[0]: r for r in db.execute("SELECT medio, estado, intento, intentos FROM fv_candidatos").fetchall()}
        meta = await asyncio.to_thread(self._meta, [m for m, _ in aptos], ahora - CAND_DIAS * DAY)
        vistas = {}
        for medio, c in aptos:
            md = meta.get(medio, {})
            vistas[medio] = cif = {**c.view(self.q0), "ruido_medio": round(self.q0, 3)}
            if medio in known:
                db.execute("UPDATE fv_candidatos SET visto=?, cifras=?, nombre=COALESCE(?, nombre), "
                           "region=COALESCE(?, region), lang=COALESCE(?, lang), host=COALESCE(?, host) WHERE medio=?",
                           (ahora, _dumps(cif), md.get("nombre"), md.get("region"), md.get("lang"), md.get("host"), medio))
            else:
                db.execute("INSERT INTO fv_candidatos VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (medio, md.get("nombre"), md.get("region"), md.get("lang"), md.get("host"), "pendiente", ahora,
                            None, 0, None, None, CAND_TXT["pendiente"], _dumps(cif), None))
                known[medio] = (medio, "pendiente", None, 0)
        db.commit()
        if not self.descubrir or self.poller is None:
            return []
        if sum(f.estado == "prueba" and f.origen == "descubierta" for f in self.filas.values()) >= MAX_EN_PRUEBA:
            return []
        added = []
        for medio, c in aptos:
            _, estado, intento, intentos = known[medio]
            md = meta.get(medio, {})
            if estado == "verificado":   # ya verificado con el automático apagado
                if self.auto:            # se encendió: se da de alta sin volver a pedir nada
                    rss, info = db.execute("SELECT rss, info FROM fv_candidatos WHERE medio=?", (medio,)).fetchone()
                    feed_id = await self._anadir(medio, md, rss, _loads(info) or {}, vistas[medio],
                                                 self._motivo_alta(vistas[medio], md))
                    res = "anadido" if feed_id else "en_registro"
                    db.execute("UPDATE fv_candidatos SET estado=?, feed=?, nota=? WHERE medio=?",
                               (res, feed_id, CAND_TXT[res], medio))
                    db.commit()
                    if feed_id:
                        added.append({"medio": medio, "feed": feed_id, "url": rss})
                continue
            wait = BLOQUEO_DIAS if estado == "bloqueado" else REINTENTO_DIAS
            if estado not in ("pendiente", "error", "bloqueado", "sin_rss") or intentos >= MAX_INTENTOS:
                continue
            if intento and ahora - intento < wait * DAY:
                continue
            if self.presupuesto < MIN_PRESUPUESTO:
                break
            try:
                res, nota, url, info = await self._buscar(medio, md.get("host"), ahora)
            except _SinPresupuesto:
                break   # sin intento gastado: sigue en la ronda siguiente
            feed_id = None
            if res == "anadido" and not self.auto:
                res, nota = "verificado", f"{nota}. Pendiente de que lo añadas: el automático está apagado"
            elif res == "anadido":
                feed_id = await self._anadir(medio, md, url, info or {}, vistas[medio], self._motivo_alta(vistas[medio], md))
                if feed_id is None:
                    res, nota = "en_registro", "Ya había un feed con esa URL"
                else:
                    added.append({"medio": medio, "feed": feed_id, "url": url})
            db.execute("UPDATE fv_candidatos SET estado=?, intento=?, intentos=intentos+1, rss=?, feed=?, nota=?, info=? "
                       "WHERE medio=?", (res, ahora, url, feed_id, nota, _dumps(info) if res == "verificado" else None,
                                         medio))
            db.commit()
            log.info("Descubrimiento %s: %s (%s)", medio, res, nota)
        return added

    def _alta(self, medio: str, md: dict, url: str, info: dict, cif: dict, motivo: str, *, auto: bool,
              por: str) -> dict | None:
        """Da de alta en prueba un feed descubierto (en marcha y en la base). Devuelve la entrada para el registro o None
        si ya había uno con ese id o esa URL."""
        region = md.get("region") if md.get("region") in regions.REGIONS else "global"
        name = md.get("nombre") or info.get("titulo") or medio
        lang = md.get("lang")
        kind = info.get("tipo") or "rss"
        spec = FeedSpec(importar_fuentes.make_id(region, url), " ".join(str(name).split())[:120], url, lang, None, region,
                        kind, TIER_NUEVA, None)
        if self.poller is None or not self.poller.add_feed(spec, "prueba"):
            return None
        self.propios.add(medio)
        self._cambiar(spec.id, "prueba", "descubierta", motivo, {**cif, "feed": info}, auto=auto, por=por,
                      origen="descubierta", spec=asdict(spec), nombre=spec.name)
        return {"id": spec.id, "name": spec.name, "url": url, "lang": lang, "country": None, "region": region,
                "kind": kind, "tier": TIER_NUEVA, "interval_s": None, "origen": importar_fuentes.DISCOVERED,
                "alta": datetime.now(timezone.utc).date().isoformat(), "medio": medio}

    def _registro(self, entry: dict) -> None:
        """Escribe el feed en bot/fuentes/<región>.json. Si falla, sigue en la base (attach() lo repone al reiniciar)."""
        try:
            importar_fuentes.anadir(entry, self.registro_dir)
        except Exception as e:
            log.warning("No se pudo escribir %s en el registro (%s): queda en la base", entry.get("id"), e)

    async def _anadir(self, medio: str, md: dict, url: str, info: dict, cif: dict, motivo: str) -> str | None:
        entry = self._alta(medio, md, url, info, cif, motivo, auto=True, por="descubrimiento")
        if entry is None:
            return None
        await asyncio.to_thread(self._registro, entry)
        return entry["id"]

    def anadir(self, medio: str, motivo: str | None = None, por: str = "tú, desde la web") -> dict:
        """Acción a mano: da de alta en prueba un candidato con su RSS ya verificado (estado «verificado»)."""
        medio = str(medio or "").strip().lower().removeprefix(MEDIO)
        row = self.store.db.execute("SELECT nombre, region, lang, host, estado, rss, cifras, info FROM fv_candidatos "
                                    "WHERE medio=?", (medio,)).fetchone() if medio else None
        if row is None:
            return {"ok": False, "error": "No conozco ese medio"}
        nombre, region, lang, host, estado, rss, cifras, info = row
        if estado != "verificado" or not rss:
            return {"ok": False, "id": medio, "estado": estado, "error": "Ese medio no tiene un RSS verificado pendiente"}
        if self.poller is None:
            return {"ok": False, "id": medio, "estado": estado, "error": "Los feeds no están en marcha"}
        md = {"nombre": nombre, "region": region, "lang": lang, "host": host}
        texto = " ".join(str(motivo or "").split())[:200]
        base = f"Añadido a mano: {texto}" if texto else "Añadido a mano"
        entry = self._alta(medio, md, rss, _loads(info) or {}, _loads(cifras) or {},
                           f"{base}. {self._motivo_alta(_loads(cifras) or {}, md)}", auto=False, por=por)
        res = "anadido" if entry else "en_registro"
        self.store.db.execute("UPDATE fv_candidatos SET estado=?, feed=?, nota=? WHERE medio=?",
                              (res, entry and entry["id"], CAND_TXT[res], medio))
        self.store.db.commit()
        if entry is None:
            self.publicar()
            return {"ok": False, "id": medio, "estado": res, "error": "Ya había un feed con esa URL"}
        try:   # el JSON del registro, fuera del bucle de eventos
            task = asyncio.get_running_loop().create_task(asyncio.to_thread(self._registro, entry))
            self._tareas.add(task)
            task.add_done_callback(self._tareas.discard)
        except RuntimeError:   # sin bucle (scripts, pruebas): en el acto
            self._registro(entry)
        self.publicar()
        return {"ok": True, "id": medio, "feed": entry["id"], "estado": "prueba"}

    # ----- ronda completa
    async def revisar(self) -> dict:
        """Mide, calcula cifras, aplica las reglas y descubre medios. Lo pesado va en hilos con conexión propia."""
        t0 = time.perf_counter()
        ahora = now_ms()
        self.presupuesto = PETICIONES_HORA
        nota = None
        self.volcar()
        medidas = await asyncio.to_thread(self._medir, ahora)
        desde = {k: f.desde for k, f in self.filas.items()}
        self.cifras, self.q0, cand = await asyncio.to_thread(self._calcular, ahora, desde, frozenset(self.propios))
        red_caida = False
        try:
            red_caida = self._salud(ahora)
        except sqlite3.Error as e:
            nota = f"No se pudo guardar la salud de los feeds: {e}"
        cambios = self.aplicar_reglas(ahora, red_caida)
        added = []
        try:
            added = await self.descubrir_medios(ahora, cand)
        except Exception as e:   # el descubrimiento no para el resto
            log.exception("El descubrimiento de medios falló")
            nota = f"El descubrimiento falló: {type(e).__name__}"
        try:
            await asyncio.to_thread(self._podar, ahora - VENTANA_DIAS * DAY)
        except sqlite3.Error as e:
            log.warning("No se pudieron podar las noticias medidas: %s", e)
        if red_caida:
            nota = "Falla más del 30 % de los feeds: parece nuestra red, no se cuenta como caída de nadie"
        self.revision = {"ultima": ahora, "s": round(time.perf_counter() - t0, 2), "medidas": medidas,
                         "cambios": len(cambios), "anadidas": len(added), "nota": nota}
        self.publicar()
        return {"medidas": medidas, "cambios": cambios, "anadidas": added}

    async def run(self) -> None:
        """24/7: guarda lo apuntado cada 30 s, lee los relays cada 10 min y revisa cada hora. Ningún fallo lo para."""
        await self.sleep(120)   # que los feeds den su primera vuelta
        next_scan = next_rev = 0.0
        while not self._closed:
            try:
                self.volcar()
                t = self.clock()
                if t >= next_scan:
                    next_scan = t + 600
                    await self.escanear_relays()
                if t >= next_rev:
                    next_rev = t + 3600
                    await self.revisar()
            except Exception:
                log.exception("Fuentes vivas: la ronda falló; se reintenta")
            await self.sleep(30)

    def close(self) -> None:
        self._closed = True
        self.volcar()

    # ----- para la web
    def estado(self) -> dict:
        """Mensaje «fuentes_vivas» del bus (sin ts)."""
        feeds = Counter(st.estado for st in self.poller.states.values()) if self.poller else Counter()
        medios = Counter(f.estado for f in self.filas.values() if f.origen == "medio")
        cand = Counter(r[0] for r in self.store.db.execute("SELECT estado FROM fv_candidatos").fetchall())
        return {"type": "fuentes_vivas", "auto": self.auto, "descubrir": self.descubrir,
                "feeds": {e: feeds.get(e, 0) for e in ESTADOS}, "medios": {e: medios.get(e, 0) for e in ESTADOS},
                "candidatos": {k: cand.get(k, 0) for k in CAND_TXT}, "ruido_medio": round(self.q0, 3),
                "sugerencias": len(self.sugerencias) + cand.get("verificado", 0), "revision": self.revision,
                "cambios": self.historial(10), "aviso": AVISO}

    def publicar(self) -> bool:
        msg = self.estado()
        key = json.dumps(msg, sort_keys=True, ensure_ascii=False, default=str)
        if key == self._last_pub:
            return False
        self._last_pub = key
        self.bus.publish({**msg, "ts": now_ms()})
        return True

    def _vista(self, clave: str, st=None) -> dict:
        ahora = now_ms()
        f = self.filas.get(clave)
        cif = self.cifras.get(clave)
        estado = st.estado if st is not None else (f.estado if f else "activa")
        sug = self.sugerencias.get(clave)
        manual = bool(f and f.manual)
        out = {"id": clave, "tipo": "medio" if clave.startswith(MEDIO) else "feed", "nombre": self._nombre(clave),
               "estado": estado, "peso": PESO_ESTADO[estado], "motivo": f.motivo if f else None,
               "cambio": f.tipo if f else None, "desde": f.desde if f else None, "manual": manual,
               "manual_hasta": f.desde + MANUAL_DIAS * DAY if manual and f.protegida(ahora) else None,
               "origen": f.origen if f else ("medio" if clave.startswith(MEDIO) else "registro"),
               "cifras": cif.view(self.q0) if cif else None, "nota": espera(f, cif, ahora),
               "sugerencia": {"estado": sug[0], "motivo": sug[2]} if sug else None,
               "acciones": {"activa": ["silenciar", "retirar"], "prueba": ["activar", "silenciar", "retirar"],
                            "silenciada": ["restaurar", "retirar"], "retirada": ["restaurar"]}[estado]}
        if st is not None:
            medio = site(st.host) if st.host and not st.gnews else None
            out.update({"region": st.spec.region, "tier": st.spec.tier, "url": st.spec.url, "medio": medio,
                        "estado_medio": (self.poller.outlet_estados.get(medio, "activa") if medio and self.poller
                                         else None),
                        "tecnico": {"ok": st.ok, "err": st.err or None, "fallos": st.fails,
                                    "horas_fallando": round(self.salud.get(clave, 0.0), 1)}})
        return out

    def lista(self, region: str | None = None, estado: str | None = None, tipo: str | None = None,
              limite: int = 10_000) -> list[dict]:
        """Fuentes con estado, peso, motivo, cifras, protección a mano y nota: primero las retiradas y silenciadas,
        luego por nombre. Los feeds son canales (técnica y ruido); los medios se juzgan con todos sus canales."""
        out = []
        if self.poller is not None and tipo in (None, "feed"):
            out += [self._vista(fid, st) for fid, st in self.poller.states.items()
                    if (not region or st.spec.region == region) and (not estado or st.estado == estado)]
        if tipo in (None, "medio") and not region:
            claves = {k for k in self.filas if k.startswith(MEDIO)}
            claves |= {k for k, c in self.cifras.items() if k.startswith(MEDIO) and c.indep >= CAND_MIN_CASOS}
            out += [v for v in (self._vista(k) for k in claves) if not estado or v["estado"] == estado]
        out.sort(key=lambda v: (-ESTADOS.index(v["estado"]), v["nombre"].casefold(), v["id"]))
        return out[:max(1, int(limite))]

    def candidatos(self, limite: int = 200) -> list[dict]:
        rows = self.store.db.execute(
            "SELECT medio, nombre, region, lang, host, estado, visto, intento, intentos, rss, feed, nota, cifras "
            "FROM fv_candidatos ORDER BY visto DESC, medio LIMIT ?", (max(1, int(limite)),)).fetchall()
        keys = ("medio", "nombre", "region", "lang", "host", "estado", "visto", "intento", "intentos", "rss", "feed",
                "nota", "cifras")
        out = []
        for r in rows:
            d = dict(zip(keys, r))
            d["cifras"] = _loads(d["cifras"])
            d["estado_texto"] = CAND_TXT.get(d["estado"], d["estado"])
            d["acciones"] = ["anadir"] if d["estado"] == "verificado" else []
            out.append(d)
        return out

    def historial(self, n: int = 200, clave: str | None = None) -> list[dict]:
        """Cambios de estado, del más reciente al más antiguo."""
        where, args = ("WHERE id=?", (clave,)) if clave else ("", ())
        rows = self.store.db.execute(f"SELECT ts, id, antes, despues, tipo, motivo, cifras, auto, por FROM "
                                     f"source_status_log {where} ORDER BY ts DESC, rowid DESC LIMIT ?",
                                     (*args, max(1, int(n)))).fetchall()
        out = []
        for ts, i, antes, despues, tipo, motivo, cifras, auto, por in rows:
            accion = _accion(antes, despues)
            out.append({"ts": ts, "id": i, "nombre": self._nombre(i), "accion": accion,
                        "texto": ACCION_TXT.get(accion, accion), "antes": antes, "despues": despues, "tipo": tipo,
                        "motivo": motivo, "cifras": _loads(cifras), "auto": bool(auto), "por": por,
                        "deshacer": {"accion": _deshacer(antes, despues), "id": i}})
        return out


def _accion(antes: str | None, despues: str) -> str:
    if antes is None:
        return "anadir"
    if despues in ("silenciada", "retirada"):
        return {"silenciada": "silenciar", "retirada": "retirar"}[despues]
    if antes in ("silenciada", "retirada"):
        return "restaurar"
    return "a_prueba" if despues == "prueba" else "activar"


def _deshacer(antes: str | None, despues: str | None = None) -> str:
    """Acción que vuelve exactamente al estado de antes (sin estado antes era un alta: se retira). Si se silenció o
    retiró desde activa o prueba, «restaurar» la devuelve justo ahí (su previo es ese «antes»)."""
    if antes in ("activa", "prueba") and despues in ("silenciada", "retirada"):
        return "restaurar"
    return DESHACER.get(antes, "retirar")


def _dumps(x) -> str | None:
    return None if x is None else json.dumps(x, ensure_ascii=False, default=str)


def _loads(s):
    if not s:
        return None
    try:
        return json.loads(s)
    except ValueError:
        return None
