"""Fuentes masivas: cientos de feeds RSS/Atom por bolsa, leídos del registro bot/fuentes/*.json.

Un único planificador (un heap ordenado por la próxima hora de sondeo) decide qué feed toca, con concurrencia global,
espaciado y tope de peticiones por host, GET condicional (ETag / If-Modified-Since, guardado en la tabla feed_state) e
intervalos que se alargan solos cuando un feed no trae nada. La misma entrada traída por varios feeds pasa una sola vez
por la mesa. Publica «sources_summary» (contrato 2.1) y da el detalle para /api/fuentes.

Ciclo de vida (lo decide bot/fuentes_vivas.py; aquí solo se respeta): una fuente retirada no se sondea; una silenciada se
sondea menos, para poder volver, y sus entradas llegan a la mesa marcadas (Item.estado, Item.peso = 0) para que no
cuenten en la señal; una en prueba se sondea normal con peso bajo. Un medio (dominio que firma la entrada) también puede
estar silenciado o retirado, llegue por su feed propio o por un agregador: sus entradas se marcan igual (el peor estado
entre el del feed y el del medio). Si una entrada repetida la trajo antes un feed con peor estado, la copia del feed con
mejor estado pasa a la mesa marcada como mejora (Item.mejora = estado de antes) para que la historia suba de estado."""
import asyncio
import hashlib
import heapq
import html
import itertools
import json
import logging
import math
import random
import re
import statistics
import time
from collections import Counter, OrderedDict, deque
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import aiohttp

from . import config, hunters, regions
from .models import Source, now_ms

log = logging.getLogger("feeds")

TIERS = ("T0", "T1", "T2", "T3")
BASE_INTERVAL_S = {"T0": 90, "T1": 120, "T2": 180, "T3": 300}
GOOGLE_NEWS_HOST = "news.google.com"
GOOGLE_NEWS_INTERVAL_S = 600
IDLE_POLLS = 3               # sondeos seguidos sin novedades antes de espaciar
IDLE_FACTOR = 1.5
IDLE_MAX_S = 900
BACKOFF_MAX_S = 1800
BACKOFF_MAX_EXP = 16         # tope del exponente: 2 ** fallos sin límite desborda tras unos mil fallos seguidos
HOST_GAP_S = {GOOGLE_NEWS_HOST: 1.2}   # Google News corta enseguida con 503 si se le pide deprisa
DEFAULT_HOST_GAP_S = 0.3
MAX_PER_HOST = 3             # peticiones a la vez por host: uno colgado no acapara la concurrencia global
HOST_TRIP = 5                # fallos de red seguidos en un host antes de frenarlo entero
HOST_BACKOFF_S = 30          # primera pausa de un host que corta (429/503) o no responde; crece ×2
THROTTLE_STATUS = (429, 503)
SUMMARY_EVERY_S = 10
START_SPREAD_S = 60          # el primer sondeo de cada feed se reparte en al menos este tiempo (más con registros grandes)
TICK_S = 0.5                 # el planificador mira el heap al menos con esta frecuencia
JITTER = 0.1
LAT_SAMPLES = 50
YIELD_EVERY = 20             # entradas seguidas por la mesa antes de ceder el bucle de eventos
# feedparser es Python puro: en hilos retiene el GIL y el bucle se para cientos de ms (convoy). Se lee en procesos aparte.
PARSE_WORKERS = 2
MAX_BYTES = 10_000_000
CHUNK = 65536
FUTURE_TOL_MS = 10 * 60_000  # margen para relojes algo adelantados
MAX_SKEW_H = 14              # ninguna zona horaria se aleja más de 14 h de UTC
DELIVERED_TTL_MS = max(config.DEDUPE_WINDOW_S, config.BACKFILL_MAX_S) * 1000
DELIVERED_MAX = 200_000
TIMEOUT = aiohttp.ClientTimeout(total=20)
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0.0.0 Safari/537.36 BotFundamental/0.1")
ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.8"
FEED_MARKS = (b"<rss", b"<feed", b"<rdf:rdf", b"<channel")
# Segundo nivel de dominios como co.jp, com.cn u or.jp: el medio es la etiqueta anterior.
SLD = {"co", "com", "or", "ne", "ac", "go", "gov", "gob", "net", "org", "edu", "gv"}
REDIRECT_SITES = {"google.com", "feedburner.com", "feedblitz.com"}   # enlaces de redirector: no dicen el medio
GN_SOURCE_RX = re.compile(rb'<source\b[^>]*\burl="([^"]+)"[^>]*>([^<]*)</source>')
# Ciclo de vida de cada fuente, de mejor a peor. El peso multiplica lo que cuentan sus noticias en la señal.
ESTADOS = ("activa", "prueba", "silenciada", "retirada")
PESO_ESTADO = {"activa": 1.0, "prueba": 0.5, "silenciada": 0.0, "retirada": 0.0}
MUTED_FACTOR = 4             # una silenciada se sondea 4 veces menos (y como poco cada 30 min): así puede volver
MUTED_MIN_S = 1800


def _rank(estado: str) -> int:
    """Orden del ciclo de vida (0 = activa, la mejor); un estado desconocido cuenta como activa."""
    return ESTADOS.index(estado) if estado in ESTADOS else 0


@dataclass(frozen=True)
class FeedSpec:
    id: str
    name: str
    url: str
    lang: str | None
    country: str | None
    region: str                  # clave de regions.REGIONS
    kind: str                    # rss, atom, google_news...
    tier: str                    # T0..T3
    interval_s: float | None = None


class FeedError(Exception):
    """Fallo propio de un feed (respuesta que no es RSS/Atom, demasiado grande...)."""


# ---------- Registro ----------
def norm_url(url: str) -> str:
    """Clave para detectar feeds repetidos: sin esquema, sin «www.» ni barra final. Google News, por consulta y edición."""
    p = urlsplit(url.strip())
    host = (p.hostname or "").lower().removeprefix("www.")
    if p.port:
        host += f":{p.port}"
    path = p.path.rstrip("/")
    if host == GOOGLE_NEWS_HOST:
        qs = parse_qs(p.query)
        q = " ".join(qs.get("q", [""])[0].lower().split())
        ed = qs.get("ceid", [""])[0] or f"{qs.get('gl', [''])[0]}:{qs.get('hl', [''])[0]}"
        return f"{host}{path}?q={q}&ceid={ed.lower()}"
    return host + path + (f"?{p.query}" if p.query else "")


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def is_google_news(spec: FeedSpec) -> bool:
    """Por tipo o por host: si el verificador lo etiqueta de otro modo, sigue yendo a 600 s."""
    return spec.kind == "google_news" or _host(spec.url) == GOOGLE_NEWS_HOST


def _spec(d: dict) -> FeedSpec:
    """Valida una entrada del registro. Lanza ValueError con el motivo."""
    if not isinstance(d, dict):
        raise ValueError("no es un objeto")
    fid = str(d.get("id") or "").strip()
    url = str(d.get("url") or "").strip()
    if not fid:
        raise ValueError("sin id")
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"url no válida: {url[:80]!r}")
    norm_url(url)   # un puerto o un host mal escrito lanza ValueError aquí y no al cargar
    region = d.get("region")
    if region not in regions.REGIONS:
        raise ValueError(f"región desconocida: {region!r}")
    tier = d.get("tier") or "T3"
    if tier not in TIERS:
        raise ValueError(f"nivel desconocido: {tier!r}")
    iv = d.get("interval_s")
    if iv is not None and (isinstance(iv, bool) or not isinstance(iv, (int, float)) or iv <= 0):
        raise ValueError(f"interval_s no válido: {iv!r}")
    name = " ".join(str(d.get("name") or "").split()) or fid
    return FeedSpec(fid, name, url, d.get("lang") or None, d.get("country") or None, region,
                    str(d.get("kind") or "rss"), tier, float(iv) if iv is not None else None)


def load_registry(dir: Path = config.FEEDS_DIR) -> list[FeedSpec]:
    """Lee todos los *.json del registro (listas de feeds). Descarta los inválidos y los repetidos por URL o id."""
    specs: list[FeedSpec] = []
    urls: set[str] = set()
    ids: set[str] = set()
    dupes = bad = 0
    for path in sorted(Path(dir).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))   # admite BOM (Bloc de notas, PowerShell)
        except (OSError, ValueError) as e:
            log.warning("Registro %s ilegible: %s", path.name, e)
            continue
        if not isinstance(data, list):
            log.warning("Registro %s: se esperaba una lista de feeds", path.name)
            continue
        for i, d in enumerate(data):
            try:
                s = _spec(d)
            except ValueError as e:
                bad += 1
                log.warning("Registro %s[%d] descartado: %s", path.name, i, e)
                continue
            key = norm_url(s.url)
            if key in urls or s.id in ids:
                dupes += 1
                continue
            urls.add(key)
            ids.add(s.id)
            specs.append(s)
    log.info("Registro de fuentes: %d feeds (%d repetidos y %d inválidos descartados)", len(specs), dupes, bad)
    return specs


def base_interval(spec: FeedSpec) -> float:
    if spec.interval_s:
        return spec.interval_s
    if is_google_news(spec):
        return GOOGLE_NEWS_INTERVAL_S
    return BASE_INTERVAL_S[spec.tier]


def short_error(e: BaseException) -> str:
    """Error en corto y en español: se muestra en la web."""
    if isinstance(e, FeedError):
        return str(e)
    if isinstance(e, aiohttp.TooManyRedirects):
        return "Demasiadas redirecciones"
    if isinstance(e, aiohttp.ClientResponseError):
        return f"HTTP {e.status}"
    if isinstance(e, asyncio.TimeoutError):
        return "Tiempo agotado (20 s)"
    if isinstance(e, aiohttp.ClientSSLError):
        return "Error de certificado o SSL"
    if isinstance(e, aiohttp.ClientConnectorError):
        return "Sin conexión con el servidor"
    if isinstance(e, aiohttp.ServerDisconnectedError):
        return "El servidor cortó la conexión"
    if isinstance(e, aiohttp.ClientPayloadError):
        return "Respuesta cortada"
    if isinstance(e, aiohttp.InvalidURL):
        return "URL no válida"
    if isinstance(e, (aiohttp.ClientConnectionError, ConnectionError)):
        return "Error de conexión"
    msg = " ".join(str(e).split())   # último recurso: el nombre técnico ayuda a buscar el fallo
    return (f"Error inesperado ({type(e).__name__}: {msg})" if msg else f"Error inesperado ({type(e).__name__})")[:120]


def retry_after(e: BaseException) -> float | None:
    """Segundos que pide el servidor en Retry-After (número o fecha HTTP)."""
    v = ((getattr(e, "headers", None) or {}).get("Retry-After") or "").strip()
    if not v:
        return None
    if v.isdigit():
        return float(v)
    try:
        d = parsedate_to_datetime(v)
    except (TypeError, ValueError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return max(0.0, d.timestamp() - time.time())


def _looks_like_feed(raw: bytes) -> bool:
    head = raw[:8192].lower()
    return any(m in head for m in FEED_MARKS)


# ---------- Medios y entradas repetidas ----------
def site(host: str) -> str:
    """Dominio del medio a partir del host: www3.nhk.or.jp → nhk.or.jp, asia.nikkei.com → nikkei.com."""
    labels = [p for p in host.lower().strip(".").split(".") if p]
    if not labels or all(p.isdigit() for p in labels):   # dirección IP
        return host
    n = 3 if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in SLD else 2
    return ".".join(labels[-n:])


def gnews_sources(raw: bytes) -> dict[str, str]:
    """Google News da el medio de cada titular en <source url="https://medio">Nombre</source>: nombre → dominio."""
    out = {}
    for url, name in GN_SOURCE_RX.findall(raw):
        host = _host(html.unescape(url.decode("utf-8", "replace")))
        n = " ".join(html.unescape(name.decode("utf-8", "replace")).split())
        if host and n:
            out[n] = site(host)
    return out


def entry_key(it, gnews: bool, host: str) -> str:
    """Clave de una entrada para reconocerla aunque llegue por otro feed: el guid en Google News, el enlace en el resto."""
    if gnews:
        return "gn|" + it.uid
    if it.url.startswith(("http://", "https://")):
        try:
            p = urlsplit(it.url)
            q = "&".join(kv for kv in p.query.split("&") if kv and not kv.lower().startswith("utm_"))
            return norm_url(p._replace(query=q).geturl())
        except ValueError:
            pass
    return f"{host}|{it.uid}"


class Delivered:
    """Entradas que algún feed ya entregó a la mesa: clave → (feed, última vez vista, mejor estado con que se entregó).
    Caduca y tiene tope."""

    def __init__(self, ttl_ms: int = DELIVERED_TTL_MS, cap: int = DELIVERED_MAX):
        self.ttl_ms, self.cap = ttl_ms, cap
        self.keys: OrderedDict[str, tuple[str, int, str]] = OrderedDict()

    def owner(self, key: str, fid: str, now: int) -> str:
        """Feed que la entregó primero; si nadie la había entregado, queda a nombre de fid."""
        return self.deliver(key, fid, now)[0]

    def deliver(self, key: str, fid: str, now: int, estado: str = "activa") -> tuple[str, str | None]:
        """(feed que la entregó primero, mejor estado con que se había entregado; None si es nueva). Queda apuntado el
        mejor estado entre el de antes y este."""
        while self.keys:
            k, (_, ts, _) = next(iter(self.keys.items()))
            if now - ts <= self.ttl_ms:
                break
            del self.keys[k]
        hit = self.keys.pop(key, None)
        first, prev = (fid, None) if hit is None else (hit[0], hit[2])
        best = estado if prev is None else min(prev, estado, key=_rank)
        self.keys[key] = (first, now, best)   # se renueva mientras algún feed la siga mostrando
        if len(self.keys) > self.cap:
            self.keys.popitem(last=False)
        return first, prev

    def __len__(self) -> int:
        return len(self.keys)


# ---------- Estado de cada feed ----------
class FeedState:
    def __init__(self, spec: FeedSpec):
        self.spec = spec
        self.host = _host(spec.url)
        self.gnews = is_google_news(spec)
        self.base = base_interval(spec)
        self.interval = self.base        # intervalo adaptativo (sin contar la espera por errores)
        self.floor_s = 0.0               # mínimo real por el espaciado de su host (p. ej. cientos de consultas a Google News)
        self.empty_polls = 0
        self.fails = 0
        self.ok: bool | None = None      # None = aún no se ha sondeado en esta ejecución
        self.err = ""
        self.last_ok: int | None = None
        self.etag: str | None = None
        self.last_modified: str | None = None
        self.digest: bytes | None = None # para no volver a leer un cuerpo idéntico cuando el servidor no da ETag
        self.uids: set[str] = set()      # entradas del último sondeo, para saber si hay novedades
        self.warmed = False
        self.skew_h = 0                  # horas que el feed adelanta sus fechas (hora local marcada como UTC)
        self.items_total = 0
        self.hits: deque[tuple[int, bool]] = deque()   # (ms, relevante) de lo que la mesa aceptó
        self.lats: deque[float] = deque(maxlen=LAT_SAMPLES)
        self.estado = "activa"           # ciclo de vida (ESTADOS); lo cambia FeedPoller.set_estado

    @property
    def peso(self) -> float:
        return PESO_ESTADO.get(self.estado, 1.0)

    def restore(self, etag, last_modified, interval_s, last_ok, fails) -> None:
        self.etag, self.last_modified, self.last_ok = etag, last_modified, last_ok
        self.fails = int(fails or 0)
        if interval_s:
            self.interval = min(max(float(interval_s), self.base), max(IDLE_MAX_S, self.base))

    def row(self) -> tuple:
        return (self.spec.id, self.etag, self.last_modified, self.interval, self.last_ok, self.fails)

    def next_wait(self) -> float:
        if self.fails:
            wait = min(self.base * 2 ** min(self.fails, BACKOFF_MAX_EXP), BACKOFF_MAX_S)
        else:
            wait = self.interval
        if self.estado == "silenciada":
            wait = max(wait * MUTED_FACTOR, MUTED_MIN_S)
        return max(wait, self.floor_s)

    def on_success(self, new: int) -> None:
        self.ok, self.err, self.fails, self.last_ok = True, "", 0, now_ms()
        if new:
            self.empty_polls, self.interval = 0, self.base
            return
        self.empty_polls += 1
        if self.empty_polls >= IDLE_POLLS:
            self.empty_polls = 0
            self.interval = min(self.interval * IDLE_FACTOR, max(IDLE_MAX_S, self.base))

    def on_error(self, err: str) -> None:
        self.ok, self.err = False, err
        self.fails += 1

    def fix_times(self, items, now: int) -> None:
        """Corrige fechas en el futuro: muchos feeds de Asia dan la hora local sin zona o marcada como GMT.

        Si dos o más entradas llegan adelantadas, el desfase del feed se estima en horas enteras (con la segunda más
        adelantada, para que una fecha suelta no lo decida) y se resta a todas. Lo que siga en el futuro queda sin hora."""
        h = 3_600_000
        future = sorted(d for d in (it.published - self.skew_h * h - now for it in items if it.published)
                        if d > FUTURE_TOL_MS)
        if len(future) >= 2:
            skew = self.skew_h + math.ceil(future[-2] / h)
            if skew <= MAX_SKEW_H:
                log.warning("Feed %s da sus fechas %d h adelantadas (hora local sin zona): se corrigen",
                            self.spec.id, skew)
                self.skew_h = skew
        for it in items:
            if it.published:
                p = it.published - self.skew_h * h
                it.published = p if p <= now + FUTURE_TOL_MS else None

    def record(self, result: str | None, published: int | None, now: int, warm: bool) -> None:
        """Apunta lo que devolvió la mesa. La latencia (publicación → captura) no cuenta el histórico del arranque."""
        if result is None:
            return
        self.items_total += 1
        self.hits.append((now, result == "captured"))
        if published and not warm:
            self.lats.append(max(0, now - published) / 1000)

    def last_hour(self, now: int) -> tuple[int, int]:
        while self.hits and self.hits[0][0] < now - 3600_000:
            self.hits.popleft()
        return len(self.hits), sum(1 for _, rel in self.hits if rel)

    def median_lat(self) -> float | None:
        return round(statistics.median(self.lats)) if self.lats else None

    def view(self, now: int | None = None) -> dict:
        items, relevant = self.last_hour(now or now_ms())
        s = self.spec
        return {"id": s.id, "name": s.name, "url": s.url, "region": s.region, "tier": s.tier, "kind": s.kind,
                "lang": s.lang, "ok": self.ok, "err": self.err or None, "last_ok": self.last_ok,
                "items_total": self.items_total, "items_last_hour": items, "relevant_last_hour": relevant,
                "interval_s": round(self.next_wait(), 1), "median_lat_s": self.median_lat(), "skew_h": self.skew_h,
                "estado": self.estado, "peso": self.peso}


# ---------- Planificación ----------
class Scheduler:
    """Heap de (próximo sondeo, orden de llegada, id): un único bucle despacha los feeds que tocan."""

    def __init__(self):
        self.heap: list[tuple[float, int, str]] = []
        self._seq = itertools.count()

    def push(self, fid: str, due: float) -> None:
        heapq.heappush(self.heap, (due, next(self._seq), fid))

    def pop_due(self, now: float) -> str | None:
        if self.heap and self.heap[0][0] <= now:
            return heapq.heappop(self.heap)[2]
        return None

    def next_due(self) -> float | None:
        return self.heap[0][0] if self.heap else None

    def __len__(self) -> int:
        return len(self.heap)


class HostGate:
    """Por host: separación mínima entre peticiones, tope de peticiones a la vez y pausa si corta o no responde."""

    def __init__(self):
        self.next: dict[str, float] = {}
        self.inflight: dict[str, int] = {}
        self.fails: dict[str, int] = {}

    @staticmethod
    def gap(host: str) -> float:
        return HOST_GAP_S.get(host, DEFAULT_HOST_GAP_S)

    def delay(self, host: str, now: float) -> float:
        return max(0.0, self.next.get(host, 0.0) - now)

    def mark(self, host: str, now: float) -> None:
        self.next[host] = max(self.next.get(host, 0.0), now + self.gap(host))

    def busy(self, host: str) -> bool:
        return self.inflight.get(host, 0) >= MAX_PER_HOST

    def start(self, host: str, now: float) -> None:
        self.inflight[host] = self.inflight.get(host, 0) + 1
        self.mark(host, now)

    def done(self, host: str) -> None:
        n = self.inflight.get(host, 0) - 1
        if n > 0:
            self.inflight[host] = n
        else:
            self.inflight.pop(host, None)

    def ok(self, host: str) -> None:
        self.fails.pop(host, None)

    def failed(self, host: str, now: float, e: BaseException) -> float:
        """Apunta el fallo de un feed del host. Devuelve la pausa impuesta al host entero (0 si no hace falta).

        429/503 frenan el host enseguida; los fallos de red o 5xx, tras HOST_TRIP seguidos. Respeta Retry-After."""
        status = e.status if isinstance(e, aiohttp.ClientResponseError) else None
        throttled = status in THROTTLE_STATUS
        network = isinstance(e, (asyncio.TimeoutError, aiohttp.ClientConnectionError)) or (status or 0) >= 500
        if not throttled and not network:
            self.fails.pop(host, None)   # el host responde: el fallo es solo de ese feed
            return 0.0
        n = self.fails[host] = self.fails.get(host, 0) + 1
        if not throttled and n < HOST_TRIP:
            return 0.0
        k = n - 1 if throttled else n - HOST_TRIP
        wait = min(max(HOST_BACKOFF_S * 2 ** min(k, BACKOFF_MAX_EXP), retry_after(e) or 0.0), BACKOFF_MAX_S)
        self.next[host] = max(self.next.get(host, 0.0), now + wait)
        return wait


# ---------- Sondeo ----------
class FeedPoller:
    def __init__(self, bus, desk, session: aiohttp.ClientSession, store, specs: list[FeedSpec],
                 estados: dict[str, str] | None = None):
        self.bus, self.desk, self.session, self.store = bus, desk, session, store
        self.states: dict[str, FeedState] = {s.id: FeedState(s) for s in specs}
        for fid, estado in (estados or {}).items():   # ciclo de vida guardado (fuentes_vivas)
            if fid in self.states and estado in ESTADOS:
                self.states[fid].estado = estado
        self.outlet_estados: dict[str, str] = {}   # medio (dominio) → estado, para los que llegan por agregadores
        self.on_item = None                         # gancho (st, item, resultado de la mesa); lo pone fuentes_vivas
        self.parked: set[str] = set()               # retiradas fuera del heap: vuelven si se restauran
        self._urls = {norm_url(st.spec.url) for st in self.states.values()}
        self._started = False
        self._hook_errors = 0
        self.sched = Scheduler()
        self.gate = HostGate()
        self.delivered = Delivered()
        self.sem = asyncio.Semaphore(config.FEEDS_CONCURRENCY)
        self.tasks: set[asyncio.Task] = set()
        self.dirty: set[str] = set()
        self._closed = False
        self._last_summary: dict | None = None
        self.parse_workers = PARSE_WORKERS   # 0 = leer en un hilo (pruebas)
        self.parse_pool: ProcessPoolExecutor | None = None   # lo crea run(); sin él se lee en un hilo
        # Se pueden sustituir en las pruebas (reloj falso).
        self.clock = time.monotonic
        self.sleep = asyncio.sleep
        self.jitter = JITTER
        self.start_spread_s = max(START_SPREAD_S, len(self.states) / config.FEEDS_CONCURRENCY * 2)
        per_host = self._refloor()
        n_gn = per_host.get(GOOGLE_NEWS_HOST, 0)
        if n_gn * self.gate.gap(GOOGLE_NEWS_HOST) > GOOGLE_NEWS_INTERVAL_S:
            log.warning("Google News: %d consultas a %.1f s entre peticiones; cada una se leerá cada %.0f s, no cada %d s",
                        n_gn, self.gate.gap(GOOGLE_NEWS_HOST), n_gn * self.gate.gap(GOOGLE_NEWS_HOST),
                        GOOGLE_NEWS_INTERVAL_S)
        store.db.execute("CREATE TABLE IF NOT EXISTS feed_state(id TEXT PRIMARY KEY, etag TEXT, last_modified TEXT, "
                         "interval_s REAL, last_ok INTEGER, fails INTEGER)")
        store.db.commit()
        for fid, etag, lm, iv, last_ok, fails in store.db.execute(
                "SELECT id, etag, last_modified, interval_s, last_ok, fails FROM feed_state").fetchall():
            if fid in self.states:
                self.states[fid].restore(etag, lm, iv, last_ok, fails)

    def _refloor(self, host: str | None = None) -> Counter:
        """Con el espaciado por host, N feeds del mismo host no pueden leerse más a menudo que cada N × hueco.
        Las retiradas no cuentan: no se sondean. Sin host, recalcula todos."""
        sts = [st for st in self.states.values() if host is None or st.host == host]
        per_host = Counter(st.host for st in sts if st.estado != "retirada")
        for st in sts:
            st.floor_s = per_host[st.host] * self.gate.gap(st.host)
        return per_host

    def _schedule_new(self, st: FeedState, spread_s: float) -> None:
        if st.estado == "retirada":
            self.parked.add(st.spec.id)
        else:
            self.sched.push(st.spec.id, self.clock() + random.uniform(0, spread_s))

    def set_estado(self, fid: str, estado: str) -> bool:
        """Cambia el ciclo de vida de un feed (lo llama fuentes_vivas). Una retirada sale del heap al tocarle; si se
        restaura, vuelve a sondearse enseguida. Devuelve False si el feed o el estado no existen."""
        st = self.states.get(fid)
        if st is None or estado not in ESTADOS:
            return False
        before, st.estado = st.estado, estado
        if (before == "retirada") != (estado == "retirada"):
            self._refloor(st.host)
        if estado != "retirada" and fid in self.parked:
            self.parked.discard(fid)
            if self._started and not self._closed:
                self.sched.push(fid, self.clock() + random.uniform(0, 30))
        return True

    def set_outlet_estado(self, outlet: str, estado: str) -> None:
        """Estado de un medio que llega por agregadores (dominio, como Item.outlet). «activa» lo quita de la lista."""
        key = (outlet or "").strip().lower()
        if not key or estado not in ESTADOS:
            return
        if estado == "activa":
            self.outlet_estados.pop(key, None)
        else:
            self.outlet_estados[key] = estado

    def add_feed(self, spec: FeedSpec, estado: str = "prueba") -> bool:
        """Añade un feed en marcha (descubierto por fuentes_vivas). False si ya hay uno con ese id o esa URL."""
        try:
            key = norm_url(spec.url)
        except ValueError:
            return False
        if spec.id in self.states or key in self._urls or estado not in ESTADOS:
            return False
        st = self.states[spec.id] = FeedState(spec)
        st.estado = estado
        self._urls.add(key)
        self._refloor(st.host)
        if self._started and not self._closed:
            self._schedule_new(st, 30)
        return True

    def has_url(self, url: str) -> bool:
        """True si algún feed (del registro o añadido en marcha) ya lee esa URL."""
        try:
            return norm_url(url) in self._urls
        except ValueError:
            return False

    def item_estado(self, st: FeedState, outlet: str | None) -> str:
        """El peor estado entre el del feed y el del medio que firma la entrada."""
        medio = self.outlet_estados.get((outlet or "").lower(), "activa")
        return max(st.estado, medio, key=ESTADOS.index)

    async def run(self) -> None:
        self._started = True
        for st in self.states.values():
            self._schedule_new(st, self.start_spread_s)
        log.info("Fuentes masivas: %d feeds, %d a la vez", len(self.states), config.FEEDS_CONCURRENCY)
        if self.parse_pool is None and self.parse_workers:
            self.parse_pool = ProcessPoolExecutor(max_workers=self.parse_workers)
        self.publish_summary()
        reporter = asyncio.create_task(self._report())
        try:
            await self._dispatch()
        finally:
            reporter.cancel()
            for t in list(self.tasks):
                t.cancel()
            self._safe_flush()
            self._stop_pool()

    def close(self) -> None:
        self._closed = True
        for t in list(self.tasks):
            t.cancel()
        self._safe_flush()
        self._stop_pool()

    def _stop_pool(self) -> None:
        pool, self.parse_pool = self.parse_pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    async def _parse(self, raw: bytes, fid: str) -> list:
        """Lee el RSS/Atom en un proceso aparte (sin convoy del GIL); sin procesos, o si el proceso cae, en un hilo."""
        pool = self.parse_pool
        if pool is not None:
            try:
                return await asyncio.get_running_loop().run_in_executor(pool, hunters.parse_rss, raw, fid)
            except BrokenProcessPool as e:   # un proceso murió: se recrea el grupo y esta lectura va en un hilo
                if pool is self.parse_pool:
                    log.warning("El proceso que lee los feeds falló (%s): se vuelve a crear", short_error(e))
                    self._stop_pool()
                    if not self._closed:
                        self.parse_pool = ProcessPoolExecutor(max_workers=max(1, self.parse_workers))
            except RuntimeError:   # grupo ya cerrado (apagando); si no, es un error del propio lector
                if pool is self.parse_pool:
                    raise
        return await asyncio.to_thread(hunters.parse_rss, raw, fid)

    async def _dispatch(self) -> None:
        while not self._closed:
            now = self.clock()
            fid = self.sched.pop_due(now)
            if fid is None:
                nxt = self.sched.next_due()
                await self.sleep(TICK_S if nxt is None else min(TICK_S, max(0.01, nxt - now)))
                continue
            st = self.states[fid]
            if st.estado == "retirada":   # no se sondea; set_estado la devuelve al heap si se restaura
                self.parked.add(fid)
                continue
            wait = self.gate.delay(st.host, now)
            if wait > 0 or self.gate.busy(st.host):   # el host aún no admite otra petición: vuelve a la cola
                self.sched.push(fid, now + (wait or self.gate.gap(st.host)))
                continue
            await self.sem.acquire()
            if self._closed:
                self.sem.release()
                break
            now = self.clock()
            wait = self.gate.delay(st.host, now)
            if wait > 0:   # mientras esperaba hueco, el host quedó en pausa
                self.sem.release()
                self.sched.push(fid, now + wait)
                continue
            self.gate.start(st.host, now)
            task = asyncio.create_task(self._poll(st))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def _poll(self, st: FeedState) -> None:
        try:
            await self.poll(st)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # un feed caído no debe tumbar al resto
            st.on_error(short_error(e))
            pause = self.gate.failed(st.host, self.clock(), e)
            if st.fails == 1 or st.fails % 10 == 0:
                log.warning("Feed %s falla (%d seguidos): %s · reintento en %.0f s",
                            st.spec.id, st.fails, st.err, st.next_wait())
            if pause:
                log.warning("Host %s corta o no responde (%s): pausa de %.0f s para todos sus feeds", st.host, st.err, pause)
        else:
            self.gate.ok(st.host)
        finally:
            self.sem.release()
            self.gate.done(st.host)
            self.dirty.add(st.spec.id)
            if not self._closed:
                try:
                    wait = st.next_wait() * random.uniform(1 - self.jitter, 1 + self.jitter)
                except Exception:   # nunca debe quedarse un feed fuera del heap
                    log.exception("No se pudo calcular la próxima espera de %s", st.spec.id)
                    wait = BACKOFF_MAX_S
                self.sched.push(st.spec.id, self.clock() + wait)

    async def _get(self, st: FeedState) -> tuple[int, bytes, str | None, str | None]:
        """GET condicional. Devuelve (estado, cuerpo, ETag, Last-Modified)."""
        headers = {"User-Agent": BROWSER_UA, "Accept": ACCEPT}
        if st.spec.lang:
            headers["Accept-Language"] = f"{st.spec.lang},en;q=0.7"
        if st.etag:
            headers["If-None-Match"] = st.etag
        if st.last_modified:
            headers["If-Modified-Since"] = st.last_modified
        async with self.session.get(st.spec.url, headers=headers, timeout=TIMEOUT) as r:
            if r.status == 304:
                return 304, b"", st.etag, st.last_modified
            r.raise_for_status()
            if (r.content_length or 0) > MAX_BYTES:
                raise FeedError("Respuesta demasiado grande")
            body = bytearray()   # por bloques: sin Content-Length (o comprimida) también tiene tope
            async for chunk in r.content.iter_chunked(CHUNK):
                body += chunk
                if len(body) > MAX_BYTES:
                    raise FeedError("Respuesta demasiado grande")
            return r.status, bytes(body), r.headers.get("ETag"), r.headers.get("Last-Modified")

    async def poll(self, st: FeedState) -> None:
        """Un sondeo: descarga, lectura y paso de cada entrada por la mesa. Si falla, lanza la excepción."""
        try:
            status, raw, etag, last_mod = await self._get(st)
            warm = not st.warmed
            digest = hashlib.blake2b(raw, digest_size=16).digest() if status != 304 else None
            if digest is not None and digest == st.digest:   # cuerpo idéntico (servidor sin ETag): no hace falta leerlo
                st.etag, st.last_modified = etag, last_mod
            if status == 304 or digest == st.digest:
                st.on_success(0)
                st.warmed = True
                return
            items = await self._parse(raw, st.spec.id)
            if not items and not _looks_like_feed(raw):
                raise FeedError("Respuesta sin RSS ni Atom")
            st.etag, st.last_modified, st.digest = etag, last_mod, digest
            st.fix_times(items, now_ms())
            sources = gnews_sources(raw) if st.gnews else {}
            src = Source(st.spec.id, st.spec.name, st.spec.tier, st.interval, None)
            new, uids = 0, set()
            for i, it in enumerate(sorted(items, key=lambda i: i.published or 0)):
                if i and i % YIELD_EVERY == 0:
                    await asyncio.sleep(0)   # cede el bucle: las peticiones en vuelo no deben agotar su tiempo
                it.region, it.lang = st.spec.region, st.spec.lang
                if st.gnews and " - " in it.title:   # «Titular - Medio»: el medio va como subfuente
                    it.title, it.via = (p.strip() for p in it.title.rsplit(" - ", 1))
                it.outlet = self._outlet(st, it, sources)
                # Para la mesa: una silenciada o retirada no cuenta en la señal, pero se sigue midiendo.
                it.estado = self.item_estado(st, it.outlet)
                it.peso = PESO_ESTADO[it.estado]
                it.mejora = None   # lo pone _first_delivery si otro feed la entregó antes con peor estado
                unseen = it.uid not in st.uids
                uids.add(it.uid)
                new += unseen
                if not self._first_delivery(st, it, unseen):
                    continue
                try:
                    res = self.desk.handle(it, src, warm)
                except Exception:
                    log.exception("La mesa no pudo procesar una entrada de %s", st.spec.id)
                    continue
                st.record(res, it.published, now_ms(), warm)
                if res is not None and self.on_item is not None:
                    self._hook(st, it, res)
            st.uids = uids
            st.on_success(new)
            st.warmed = True
        finally:
            self.dirty.add(st.spec.id)   # al final: un flush durante la petición no debe saltarse este resultado

    def _hook(self, st: FeedState, it, res: str) -> None:
        """Avisa a fuentes_vivas de lo que la mesa hizo con la entrada. Un fallo del gancho no para el sondeo."""
        try:
            self.on_item(st, it, res)
        except Exception:
            self._hook_errors += 1
            if self._hook_errors in (1, 100) or self._hook_errors % 10_000 == 0:
                log.exception("El gancho de fuentes vivas falló (%d veces)", self._hook_errors)

    @staticmethod
    def _outlet(st: FeedState, it, sources: dict[str, str]) -> str:
        """Medio que firma la entrada (dominio o, si no se sabe, nombre): dos feeds del mismo medio no se confirman."""
        if st.gnews:
            if it.via:
                via = " ".join(it.via.split())
                return sources.get(via) or via.casefold()
            return st.spec.id
        for h in (_host(it.url), st.host):
            s = site(h) if h else ""
            if s and s not in REDIRECT_SITES:
                return s
        return st.spec.id

    def _first_delivery(self, st: FeedState, it, unseen: bool) -> bool:
        """True si ningún otro feed ha entregado ya esta entrada a la mesa, o si la entregó con peor estado.

        Si otro la trajo antes (varias consultas de Google News, categorías de un mismo medio), no es una confirmación
        independiente: no pasa por la mesa, pero se marca como vista con la clave de la mesa para que tras un reinicio
        no salga como nueva por este feed. Excepción: si la trajo antes un feed silenciado o en prueba y ahora llega por
        uno con mejor estado, pasa como MEJORA DE ESTADO (it.mejora = el estado de antes): la mesa no debe crear otra
        historia, sino poner a la que ya existe el estado y el peso de esta (srcState, srcWeight)."""
        now = now_ms()
        estado = getattr(it, "estado", None) or st.estado
        first, prev = self.delivered.deliver(entry_key(it, st.gnews, st.host), st.spec.id, now, estado)
        if first == st.spec.id:
            return True
        if prev is not None and _rank(estado) < _rank(prev):
            it.mejora = prev
            return True
        if unseen:   # solo la primera vez que este feed la lista, para no consultar la base en cada sondeo
            key = f"{it.source}|{it.uid}"   # misma clave que desk.handle
            try:
                if not self.store.is_seen(key):
                    self.store.mark_seen(key, now)
            except Exception:
                log.exception("No se pudo marcar como vista una entrada repetida de %s", st.spec.id)
        return False

    # ---------- Estado, resumen y API ----------
    def flush(self) -> None:
        """Guarda ETag, Last-Modified e intervalo de los feeds que han cambiado."""
        if not self.dirty:
            return
        rows = [self.states[i].row() for i in self.dirty if i in self.states]
        self.dirty.clear()
        self.store.db.executemany("INSERT OR REPLACE INTO feed_state VALUES (?,?,?,?,?,?)", rows)
        self.store.db.commit()

    def _safe_flush(self) -> None:
        try:
            self.flush()
        except Exception as e:
            log.warning("No se pudo guardar el estado de los feeds: %s", e)

    async def _report(self) -> None:
        while True:
            await asyncio.sleep(SUMMARY_EVERY_S)
            try:
                self.flush()
                self.publish_summary()
            except Exception:
                log.exception("Fallo al guardar o publicar el resumen de fuentes")

    @staticmethod
    def _counts(sts: list[FeedState], now: int) -> dict:
        c = {"feeds": len(sts), "ok": 0, "failing": 0, "waiting": 0, "items_last_hour": 0, "relevant_last_hour": 0}
        for st in sts:
            c["waiting" if st.ok is None else "ok" if st.ok else "failing"] += 1
            items, relevant = st.last_hour(now)
            c["items_last_hour"] += items
            c["relevant_last_hour"] += relevant
        return c

    def summary(self) -> dict:
        """Las retiradas no cuentan: no se sondean (saldrían siempre «esperando»)."""
        now = now_ms()
        alive = [st for st in self.states.values() if st.estado != "retirada"]
        by: dict[str, list[FeedState]] = {}
        for st in alive:
            by.setdefault(st.spec.region, []).append(st)
        out = []
        for key, (name, market) in regions.REGIONS.items():
            sts = by.get(key)
            if not sts:
                continue
            lats = [m for m in (st.median_lat() for st in sts) if m is not None]
            out.append({"region": key, "name": name, "market": market, **self._counts(sts, now),
                        "median_lat_s": round(statistics.median(lats)) if lats else None})
        return {"totals": self._counts(alive, now), "regions": out}

    def publish_summary(self) -> bool:
        """Publica «sources_summary» solo si ha cambiado desde la última vez."""
        s = self.summary()
        if s == self._last_summary:
            return False
        self._last_summary = s
        self.bus.publish({"type": "sources_summary", "ts": now_ms(), **s})
        return True

    def api_list(self, region: str | None = None) -> list[dict]:
        """Detalle por feed para GET /api/fuentes?region=."""
        now = now_ms()
        sts = [st for st in self.states.values() if not region or st.spec.region == region]
        sts.sort(key=lambda st: (st.spec.region, st.spec.tier, st.spec.name.casefold()))
        return [st.view(now) for st in sts]
