"""Arranque: python -m bot  →  web en http://127.0.0.1:8765

Además de noticias, mercado, paper y aperturas, arrancan los módulos que miden y aprenden (todo solo paper):
vigilancia (prueba en avance de las estrategias congeladas), aprendizaje (medida de noticias y medios, pesos y
post-mortems), fuentes vivas (estado de cada fuente y descubrimiento), evolución (historial, campeón y aspirantes,
propuestas e informe semanal), reacción (la cripto en cada apertura de bolsa: «cripto_aperturas»), reflejo (¿la cripto
refleja las noticias macro y geopolíticas?), noticias fiables (los grupos que aciertan ≥ 60 % de forma constante) y la
entrada por noticia fiable confirmada por el precio. Si uno no arranca, el bot sigue sin él (optional), y si uno se
cae mientras corre, se relanza con espera creciente sin parar al resto (supervise; hallazgos n.º 27 y 46)."""
import asyncio
import logging
import os
import ssl
import sys
import tempfile
import time
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from zoneinfo import ZoneInfo

import aiohttp
import truststore
from aiohttp import web

from . import agenda, config
from .apertura import PAIRS, OpeningEngine, pick_strategy, signal_getter
from .aprendizaje import Aprendizaje, CacheBars
from .backtest import Backtester
from .backtest_aperturas import fetch_api as klines_1m
from .bus import Bus
from .claude_agents import Phase2
from .desk import NewsDesk
from .entrada_noticias import ENABLED as ENTRADA_NOTICIAS, EntradaNoticias
from .evolucion import ARCHIVO as ESTRATEGIA_ACTIVA, Evolucion
from .feeds import FeedPoller, load_registry
from .fiables import ENABLED as FIABLES, Fiables
from .fuentes_vivas import FuentesVivas
from .hunters import SOURCES, SourceState, run_source
from .local_ai import LocalAI
from .market import Market
from .models import now_ms
from .paper import PaperBroker
from .predict import Predictor, results_getter
from .reaccion import Reaccion
from .reflejo import Reflejo
from .reloj import Reloj
from .salud import Salud, proceso
from .server import Services, make_app
from .store import Store, copia_diaria, in_batches
from .trend import Trend
from .vigilancia import Vigilancia

log = logging.getLogger("bot")


def data_tmp() -> None:
    """Temporales del proceso (también los de SQLite: ordenaciones y tablas temporales del aprendizaje) en M:."""
    try:
        config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("No pude crear %s (%s): los temporales siguen donde estaban", config.TMP_DIR, e)
        return
    for var in ("TMP", "TEMP", "SQLITE_TMPDIR"):
        os.environ[var] = str(config.TMP_DIR)
    tempfile.tempdir = str(config.TMP_DIR)


_JOB = None   # el Job Object de Windows: se queda abierto mientras viva el proceso


def atar_hijos(k32=None) -> bool:
    """Windows: mete el proceso en un Job Object con KILL_ON_JOB_CLOSE. Los lectores de feeds (ProcessPoolExecutor) y
    llama-server lo heredan, así que si el bot muere de golpe Windows los cierra con él y no quedan huérfanos ocupando
    VRAM o esperando su cola (hallazgo n.º 49). En Linux, o con BOT_JOB_OBJECT=0, no hace nada. Si Windows no deja
    (el proceso ya está en un trabajo que no admite otro), se registra y el bot sigue como antes."""
    global _JOB
    if os.environ.get("BOT_JOB_OBJECT", "1") == "0" or (k32 is None and os.name != "nt"):
        return False
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):   # JOBOBJECT_BASIC_LIMIT_INFORMATION
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),   # DWORD = 32 bits
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32), ("SchedulingClass", ctypes.c_uint32)]

    class Extended(ctypes.Structure):   # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    err = getattr(ctypes, "get_last_error", lambda: 0)   # solo existe en Windows
    try:
        if k32 is None:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = wintypes.HANDLE   # sin esto, un HANDLE de 64 bits se cortaría a 32
            k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
            k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
            k32.SetInformationJobObject.restype = wintypes.BOOL
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
            k32.AssignProcessToJobObject.restype = wintypes.BOOL
        job = k32.CreateJobObjectW(None, None)
        if not job:
            raise OSError(f"CreateJobObject: error {err()}")
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):   # 9: ExtendedLimit
            raise OSError(f"SetInformationJobObject: error {err()}")
        if not k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
            raise OSError(f"AssignProcessToJobObject: error {err()}")
    except Exception as e:
        log.warning("No se pudo atar los procesos hijos al bot (%s): si el bot muere de golpe, pueden quedar abiertos", e)
        return False
    _JOB = job
    log.info("Procesos hijos atados al bot: si se cierra, Windows los cierra con él")
    return True


def optional(name: str, make):
    """Construye un módulo que no es imprescindible: si falla, queda registrado y el bot sigue sin él."""
    try:
        return make()
    except Exception:
        log.exception("%s no arrancó: el bot sigue sin este módulo", name)
        return None


SUPERVISE_MIN_S, SUPERVISE_MAX_S = 5, 300   # espera antes de relanzar un bucle caído (crece al repetirse)
SUPERVISE_OK_S = 600                        # si llevaba esto en marcha, la espera vuelve a la mínima


async def supervise(name: str, factory, min_s: float = SUPERVISE_MIN_S, max_s: float = SUPERVISE_MAX_S) -> None:
    """Ejecuta factory() (crea la corrutina de un bucle) y, si sale con un error, lo registra y la relanza con espera
    creciente: un fallo suelto en un módulo (p. ej. «database is locked») ya no tumba el proceso entero (hallazgo
    n.º 46; antes asyncio.gather paraba el bot con la primera excepción). Si termina sin error, no se relanza."""
    wait = min_s
    while True:
        t0 = time.monotonic()
        try:
            await factory()
            log.info("%s terminó", name)
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            if time.monotonic() - t0 >= SUPERVISE_OK_S:
                wait = min_s
            log.exception("%s se cayó: se relanza en %.0f s (el resto del bot sigue)", name, wait)
        await asyncio.sleep(wait)
        wait = min(max_s, wait * 2)


def db_health(path, carpeta) -> dict:
    """Tamaño de bot.db y de su WAL en MB y la última copia diaria (para /health; hallazgos n.º 39 y 51)."""
    def mb(p) -> float | None:
        try:
            return round(os.path.getsize(p) / 1e6, 1)
        except OSError:
            return None
    try:
        copias = sorted(p.name for p in carpeta.glob("bot-????-??-??.db"))
    except OSError:
        copias = []
    return {"mb": mb(path), "wal_mb": mb(f"{path}-wal"), "copias": len(copias), "ultima_copia": copias[-1] if copias else None}


def observe_bus(bus: Bus, fn) -> None:
    """Gancho síncrono tras cada mensaje del bus (el aprendizaje anota al momento el medio de cada captada)."""
    publish = bus.publish

    def publish_and_observe(msg: dict) -> dict:
        out = publish(msg)
        fn(msg)   # no lanza (lo garantiza el gancho)
        return out

    bus.publish = publish_and_observe


def setup_logging() -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Un archivo por día, LOG_KEEP_DAYS días (n.º 50): con 5 MB × 5 solo cabían unos 4 días (unos 7,3 MB al día)
    fh = TimedRotatingFileHandler(config.LOG_DIR / "bot.log", when="midnight", backupCount=config.LOG_KEEP_DAYS,
                                  encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh])
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


async def maintenance(store: Store) -> None:
    """Cada hora: borra los descartes viejos, las captadas de más de KEEP_CAPTURED_S (7 días) y demás eventos masivos
    de más de 30 días y las claves de piezas ya vistas que ningún feed va a repetir. Una vez al día (día de Madrid):
    copia en caliente de bot.db en DATA_DIR/copias (7 copias; hallazgo n.º 39) y PRAGMA optimize (n.º 51)."""
    hecho = None
    while True:
        try:
            now = now_ms()
            gone = await in_batches(store.prune, ("filtered",), now - config.KEEP_FILTERED_S * 1000)
            cap = await in_batches(store.prune, config.PRUNE_CAPTURED_KINDS, now - config.KEEP_CAPTURED_S * 1000)
            old = await in_batches(store.prune, config.PRUNE_EVENT_KINDS, now - config.KEEP_EVENTS_S * 1000)
            seen = await in_batches(store.prune_seen, now - config.KEEP_SEEN_S * 1000)
            if gone or cap or old or seen:
                log.info("Limpieza: %d descartes, %d captadas, %d eventos antiguos y %d claves vistas borrados",
                         gone, cap, old, seen)
        except Exception:
            log.exception("La limpieza de la base falló")
        dia = datetime.now(ZoneInfo("Europe/Madrid")).date().isoformat()
        if hecho != dia:
            try:
                dest = await asyncio.to_thread(copia_diaria, store.path, config.COPIAS_DIR, dia)
                if dest is not None:
                    log.info("Copia de bot.db hecha en %s (%.0f MB)", dest, dest.stat().st_size / 1e6)
                store.db.execute("PRAGMA optimize")
                hecho = dia
            except Exception:
                log.exception("La copia diaria de bot.db falló: se reintenta en la próxima ronda")
        await asyncio.sleep(3600)


async def main() -> None:
    setup_logging()
    atar_hijos()
    data_tmp()
    store = Store(config.DB_PATH)
    bus = Bus(store)
    states = [SourceState(s) for s in SOURCES]

    # Certificados del sistema (Windows): sin ellos fallan muchas webs oficiales asiáticas (.go.jp, .gov.hk…).
    tls = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    timeout = aiohttp.ClientTimeout(total=20)
    connector = aiohttp.TCPConnector(ssl=tls, limit=config.FEEDS_CONCURRENCY + 60, ttl_dns_cache=600)
    async with aiohttp.ClientSession(headers={"User-Agent": config.USER_AGENT}, timeout=timeout, connector=connector) as session:
        local = LocalAI(bus, session)
        desk = NewsDesk(bus, store, on_relevant=local.submit)
        market = Market(bus, session)
        trend = Trend(bus, session)
        feeds = FeedPoller(bus, desk, session, store, load_registry())
        backtester = Backtester(bus, session)
        reloj = Reloj(bus, session)   # el desfase de la hora frente a OKX (n.º 53)
        bt = results_getter()   # resultados del backtest con caché por fecha del archivo (pesa varios MB)
        state = bus.state.get
        predictor = Predictor(bus, store, lambda: state("trend"), lambda: state("orderflow"), lambda: state("market"), bt,
                              get_reaccion=lambda: state("cripto_aperturas"))
        async def velas_hueco(coin: str, a: int, b: int) -> list[list]:
            """Velas de 1 min de Binance de [a, b) para reconstruir el hueco de una posición paper (n.º 47)."""
            return await klines_1m(session, PAIRS[coin], a, b, now_ms())

        # day_limits: los topes del día del preset del motor (se lee al enviar cada ticket; el motor se crea después)
        broker = PaperBroker(bus, store, lambda: state("market", {}), lambda: state("orderflow", {}),
                             fetch_bars=velas_hueco, day_limits=lambda: getattr(openings, "p", None))
        phase2 = Phase2(bus, store, session, local, lambda: state("predictions"), lambda: state("trend"),
                        lambda: state("orderflow"), lambda: state("market"), bt, broker.portfolio, broker.submit)
        # Motor de aperturas en modo impulso (la operativa del usuario: seguir el impulso de los primeros minutos,
        # respaldado por las noticias y la apertura anterior). BOT_APERTURAS_ESTRATEGIA=ruptura vuelve a la v2.
        # En modo impulso manda la estrategia que aplique la evolución (DATA_DIR/estrategia_activa.json), si cumple las
        # reglas del usuario; si no hay archivo, el preset.
        os.environ.setdefault("BOT_APERTURAS_ESTRATEGIA", "impulso")
        activa = config.DATA_DIR / ESTRATEGIA_ACTIVA if pick_strategy(None) == "impulso" else None
        openings = OpeningEngine(bus, session, signal_getter(bus), broker.submit, broker.portfolio, activa_path=activa)
        log.info("Motor de aperturas en modo %s (parámetros: %s)", getattr(openings, "strategy", "ruptura"),
                 getattr(openings, "source", "?"))
        # Vigilancia: prueba en avance (sin operar) de las estrategias congeladas del laboratorio. Si no arranca, el resto
        # sigue con las velas de la caché (hallazgo n.º 27).
        vigil = optional("La vigilancia", lambda: Vigilancia(bus, store, session))

        def live_bars(c: str):
            """Velas de 1 min que la vigilancia tiene en memoria (None sin vigilancia: solo la caché)."""
            return vigil.bars.get(c) if vigil is not None else None

        # Aprendizaje: mide cada noticia y cada medio con las velas de 1 min (las de la vigilancia en memoria y la caché).
        learn = optional("El aprendizaje", lambda: Aprendizaje(
            bus, store, local_ai=local, bars=CacheBars(live=live_bars)))
        if learn is not None:
            observe_bus(bus, learn.observe)
        # Reflejo: una noticia macro o geopolítica solo cuenta si la cripto la confirma (el mayor de 0,5 % y 2σ de 60 min,
        # en 60 min). Recibe las captadas y las triadas por el gancho del bus; velas de la vigilancia y la caché.
        refl = optional("El reflejo", lambda: Reflejo(bus, bars=CacheBars(live=live_bars).get))
        if refl is not None:
            observe_bus(bus, refl.observe)
        # Noticias fiables: los grupos (medio, feed, tipo×sentido, medio×sentido) que aciertan ≥ 60 % de forma constante,
        # con lo que midió el aprendizaje (news_outcomes); al arrancar y cada hora. Los usan el veto de «6 diarias» y la
        # entrada por noticia, solo con los grupos que pasan la puerta para operar (sin ellos, ninguno hace nada).
        fiab = optional("Las noticias fiables", lambda: Fiables(bus, store)) if FIABLES else None
        # Entrada por noticia fiable: espera a que el precio la confirme y entra en el paper (stop 1,5 %, 3R, 24 h) con
        # los topes diarios compartidos con el motor. BOT_ENTRADA_NOTICIAS=0 la apaga.
        entrada = None
        if ENTRADA_NOTICIAS:
            entrada = optional("La entrada por noticia fiable", lambda: EntradaNoticias(
                bus, store, broker.submit, broker.portfolio, bars=CacheBars(live=live_bars).get,
                get_motor=lambda: getattr(openings, "p", None)))
        if entrada is not None:
            observe_bus(bus, entrada.observe)
        # Reacción: la cripto en cada apertura de bolsa (cripto_aperturas); la historia, en un hilo y una vez al día.
        react = optional("La reacción de la cripto en las aperturas", lambda: Reaccion(
            bus, bars=CacheBars(live=live_bars).get))
        # Fuentes vivas: estado de cada fuente (lo respetan feeds y la mesa) y descubrimiento. Antes de feeds.run().
        vivas = optional("Fuentes vivas", lambda: FuentesVivas(bus, store, session))
        if vivas is not None:
            try:
                vivas.attach(feeds)
            except Exception:
                log.exception("Fuentes vivas no pudo engancharse a los feeds: el bot sigue sin ellas")
                vivas = None
        # Evolución: historial de cambios (fuentes, pesos, estrategia), campeón frente a aspirantes y propuestas.
        evo = optional("La evolución", lambda: Evolucion(bus, store))
        if evo is not None:
            if vivas is not None:
                optional("El enlace evolución–fuentes", lambda: evo.conectar_fuentes(vivas))
            if learn is not None:
                optional("El enlace evolución–aprendizaje", lambda: evo.conectar_aprendizaje(learn))
            # El cambio del filtro de noticias (28-10-2026) entra con este reinicio: queda apuntado una sola vez.
            optional("El apunte de los cambios del código", evo.apuntar_cambios_codigo)

        def health() -> dict:
            return {"phase": config.PHASE, "sources": [st.view() for st in states],
                    "market": {"ok": market.ok, "err": market.err, "prices": market.prices},
                    "trend": {"ok": trend.ok, "err": trend.err}, "feeds": feeds.summary(),
                    "local_ai": {"queue": local.q.qsize(), "done": local.done, "dropped": local.dropped},
                    "db": db_health(store.path, config.COPIAS_DIR),
                    "arranque": bus.started, "en_marcha_s": round((now_ms() - bus.started) / 1000),
                    "hueco_al_arrancar": bus.hueco, "reloj": reloj.ultimo, "proceso": proceso(),
                    "retraso_bucle": salud.retraso, "clients": len(bus.clients)}

        salud = Salud(health, config.LOG_DIR / "salud.csv")   # cada 5 min, una fila para la prueba de 72 h (n.º 40)

        services = Services(feeds=feeds, predictor=predictor, broker=broker, phase2=phase2, backtest=bt, openings=openings,
                            vigilancia=vigil, aprendizaje=learn, evolucion=evo, fuentes_vivas=vivas, reaccion=react,
                            reflejo=refl, fiables=fiab, entrada_noticias=entrada)
        runner = web.AppRunner(make_app(bus, health, services))
        await runner.setup()
        await web.TCPSite(runner, config.HOST, config.PORT).start()
        log.info("Web en http://%s:%d · datos en %s", config.HOST, config.PORT, config.DATA_DIR)
        bus.publish({"type": "agent", "agent": "hunter",
                     "patch": {"now": f"Arrancando {len(states)} fuentes directas y {len(feeds.states)} feeds…"}})

        async def report_sources() -> None:
            last = None
            while True:
                view = [st.view() for st in states]
                view += [{"id": "okx_prices", "name": "Precios OKX EU", "tier": "Mercado", "n": len(market.prices),
                          "ok": market.ok["prices"], "err": market.err["prices"], "lat": None},
                         {"id": "flow", "name": "Posicionamiento (Binance, Coinbase)", "tier": "Mercado", "n": len(market.flow),
                          "ok": market.ok["flow"], "err": market.err["flow"], "lat": None}]
                view += trend.sources()
                key = [(v["n"], v["ok"], v["err"], v["lat"]) for v in view]
                if key != last:
                    last = key
                    bus.publish({"type": "sources", "sources": view})
                await asyncio.sleep(3)

        # Cada bucle, supervisado: si uno se cae se relanza y el resto sigue (n.º 46). Cada run() atrapa ya sus fallos
        # habituales; supervise es la red para lo que se escape.
        tareas = [(f"La fuente {st.src.name}", lambda st=st: run_source(st, session, desk)) for st in states]
        tareas += [("El mercado", market.run), ("La agenda", lambda: agenda.run(bus, session)), ("La IA local", local.run),
                   ("El resumen de fuentes", report_sources), ("La tendencia", trend.run), ("Los feeds", feeds.run),
                   ("El backtest", backtester.run), ("Las predicciones", predictor.run), ("El paper", broker.run),
                   ("La fase 2", phase2.run), ("El motor de aperturas", openings.run),
                   ("La limpieza de la base", lambda: maintenance(store)), ("El reloj", reloj.run),
                   ("La salud", salud.run)]
        tareas += [(nombre, mod.run) for nombre, mod in (
            ("La vigilancia", vigil), ("El aprendizaje", learn), ("Las fuentes vivas", vivas), ("La evolución", evo),
            ("El reflejo", refl), ("La reacción", react), ("Las noticias fiables", fiab),
            ("La entrada por noticia", entrada)) if mod is not None]
        try:
            await asyncio.gather(*(supervise(nombre, fab) for nombre, fab in tareas))
        except Exception:
            log.exception("El bot se detuvo por un error no controlado")   # que la causa quede en bot.log
            raise
        finally:
            feeds.close()
            if vivas is not None:   # lo apuntado de las fuentes, a la base (antes de cerrarla)
                try:
                    vivas.close()
                except Exception:
                    log.exception("Fuentes vivas no pudo guardar lo pendiente al cerrar")
            try:   # las velas nuevas de la vigilancia, a la caché
                if vigil is not None:
                    await vigil.close()
            except Exception:
                log.exception("La vigilancia no pudo guardar sus velas al cerrar")
            await phase2.close()
            local.close()
            await runner.cleanup()
            store.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
