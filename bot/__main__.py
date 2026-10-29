"""Arranque: python -m bot  →  web en http://127.0.0.1:8765

Además de noticias, mercado, paper y aperturas, arrancan los módulos que miden y aprenden (todo solo paper):
vigilancia (prueba en avance de las estrategias congeladas), aprendizaje (medida de noticias y medios, pesos y
post-mortems), fuentes vivas (estado de cada fuente y descubrimiento), evolución (historial, campeón y aspirantes,
propuestas e informe semanal), reacción (la cripto en cada apertura de bolsa: «cripto_aperturas»), reflejo (¿la cripto
refleja las noticias macro y geopolíticas?), noticias fiables (los grupos que aciertan ≥ 60 % de forma constante), la
entrada por noticia fiable confirmada por el precio y las decisiones de las aperturas (por qué entra o no, tabla
decisiones_apertura). Si uno no arranca, el bot sigue sin él.
Modo real con OKX EU (docs/06): el conector (RealBroker) se construye siempre y queda APAGADO sin las claves del usuario
(secretos/okx.env): no hace ninguna petición privada. Los tickets van al paper (BOT_BROKER=paper, por defecto); con
BOT_BROKER=real irían al conector, que además necesita las claves, BOT_MODO_REAL=1 y la confirmación escrita en la web.
Comisiones (bot/comisiones.py): el perfil de OKX EU en uso; si el conector lee la comisión real de la cuenta (solo
lectura), manda esa. La gráfica de cada operación usa las velas públicas de OKX EU del par (bot/velas_okx.py)."""
import asyncio
import logging
import os
import ssl
import sys
import tempfile
from logging.handlers import RotatingFileHandler

import aiohttp
import truststore
from aiohttp import web

from . import agenda, comisiones, config
from .apertura import OpeningEngine, pick_strategy, signal_getter
from .aprendizaje import Aprendizaje, CacheBars
from .backtest import Backtester
from .bus import Bus
from .claude_agents import Phase2
from .decisiones import Decisiones
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
from .okx_firma import FiltroSecretos
from .okx_real import RealBroker
from .paper import PaperBroker
from .predict import Predictor, results_getter
from .reaccion import Reaccion
from .reflejo import Reflejo
from .server import Services, make_app, velas_de
from .store import Store, in_batches
from .trend import Trend
from .velas_okx import VelasOKX
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


def optional(name: str, make):
    """Construye un módulo que no es imprescindible: si falla, queda registrado y el bot sigue sin él."""
    try:
        return make()
    except Exception:
        log.exception("%s no arrancó: el bot sigue sin este módulo", name)
        return None


def observe_bus(bus: Bus, fn) -> None:
    """Gancho síncrono tras cada mensaje del bus (el aprendizaje anota al momento el medio de cada captada)."""
    publish = bus.publish

    def publish_and_observe(msg: dict) -> dict:
        out = publish(msg)
        fn(msg)   # no lanza (lo garantiza el gancho)
        return out

    bus.publish = publish_and_observe


def log_handlers() -> list[logging.Handler]:
    """Registro del bot: archivo rotativo y consola. Los dos con FiltroSecretos (okx_firma), no solo el logger
    «okx_real»: una excepción que suba desde el cliente de OKX hasta otro módulo (server, el arranque…) sale igual de
    enmascarada."""
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = RotatingFileHandler(config.LOG_DIR / "bot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    sh = logging.StreamHandler(sys.stdout)
    for h in (fh, sh):
        h.setFormatter(fmt)
        h.addFilter(FiltroSecretos())
    return [fh, sh]


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, handlers=log_handlers())
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


async def maintenance(store: Store) -> None:
    """Cada hora: borra los descartes viejos, las captadas y demás eventos masivos de más de 30 días y las claves de
    piezas ya vistas que ningún feed va a repetir."""
    while True:
        try:
            now = now_ms()
            gone = await in_batches(store.prune, ("filtered",), now - config.KEEP_FILTERED_S * 1000)
            old = await in_batches(store.prune, config.PRUNE_EVENT_KINDS, now - config.KEEP_EVENTS_S * 1000)
            seen = await in_batches(store.prune_seen, now - config.KEEP_SEEN_S * 1000)
            if gone or old or seen:
                log.info("Limpieza: %d descartes, %d eventos antiguos y %d claves vistas borrados", gone, old, seen)
        except Exception:
            log.exception("La limpieza de la base falló")
        await asyncio.sleep(3600)


async def main() -> None:
    setup_logging()
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
        bt = results_getter()   # resultados del backtest con caché por fecha del archivo (pesa varios MB)
        state = bus.state.get
        predictor = Predictor(bus, store, lambda: state("trend"), lambda: state("orderflow"), lambda: state("market"), bt,
                              get_reaccion=lambda: state("cripto_aperturas"))
        # Por qué entra o no en cada apertura (decisiones_apertura): lo apuntan el motor y, por el gancho del bus, el
        # riesgo, el paper y la entrada por noticia. Se engancha antes de que el paper publique nada.
        decs = optional("Las decisiones de las aperturas", lambda: Decisiones(bus, store))
        if decs is not None:
            observe_bus(bus, decs.observe)
        broker = PaperBroker(bus, store, lambda: state("market", {}), lambda: state("orderflow", {}))
        # Modo real con OKX EU (docs/06, contrato de cableado): sin secretos/okx.env queda apagado y no hace ninguna
        # petición privada. Con las claves del usuario lee su cuenta en solo lectura (saldo y comisiones); opera solo con
        # BOT_MODO_REAL=1 y su confirmación escrita en la web. Mismo mercado del bus que el paper.
        real = optional("El conector del modo real", lambda: RealBroker(
            bus, store, lambda: state("market", {}), session=session, get_orderflow=lambda: state("orderflow", {})))
        # La comisión real de la cuenta (cuenta_real, solo lectura) manda en el perfil de comisiones del paper.
        observe_bus(bus, comisiones.observar)
        # Quién recibe los tickets del motor de aperturas y de la entrada por noticia: el paper (BOT_BROKER=paper, por
        # defecto) o el conector real (BOT_BROKER=real). La fase 2 siempre va al paper.
        if config.BROKER not in ("paper", "real"):
            log.warning("BOT_BROKER=%s no es paper ni real: los tickets van al paper", config.BROKER)
        usar_real = config.BROKER == "real" and real is not None
        if config.BROKER == "real" and real is None:
            log.error("BOT_BROKER=real pero el conector del modo real no arrancó: los tickets van al paper")
        ticket_submit, ticket_pf = (real.submit, real.portfolio) if usar_real else (broker.submit, broker.portfolio)
        log.info("Tickets del motor y de la entrada por noticia: %s · comisiones: %s (%s)",
                 "conector real de OKX EU" if usar_real else "paper", comisiones.activo(), comisiones.texto())
        phase2 = Phase2(bus, store, session, local, lambda: state("predictions"), lambda: state("trend"),
                        lambda: state("orderflow"), lambda: state("market"), bt, broker.portfolio, broker.submit)
        # Motor de aperturas en modo impulso (la operativa del usuario: seguir el impulso de los primeros minutos,
        # respaldado por las noticias y la apertura anterior). BOT_APERTURAS_ESTRATEGIA=ruptura vuelve a la v2.
        # En modo impulso manda la estrategia que aplique la evolución (DATA_DIR/estrategia_activa.json), si cumple las
        # reglas del usuario; si no hay archivo, el preset.
        os.environ.setdefault("BOT_APERTURAS_ESTRATEGIA", "impulso")
        activa = config.DATA_DIR / ESTRATEGIA_ACTIVA if pick_strategy(None) == "impulso" else None
        openings = OpeningEngine(bus, session, signal_getter(bus), ticket_submit, ticket_pf, activa_path=activa,
                                 decisiones=decs)
        log.info("Motor de aperturas en modo %s (parámetros: %s)", getattr(openings, "strategy", "ruptura"),
                 getattr(openings, "source", "?"))
        # Vigilancia: prueba en avance (sin operar) de las estrategias congeladas del laboratorio.
        vigil = Vigilancia(bus, store, session)
        # Aprendizaje: mide cada noticia y cada medio con las velas de 1 min (las de la vigilancia en memoria y la caché).
        learn = optional("El aprendizaje", lambda: Aprendizaje(
            bus, store, local_ai=local, bars=CacheBars(live=lambda c: vigil.bars.get(c))))
        if learn is not None:
            observe_bus(bus, learn.observe)
        # Reflejo: una noticia macro o geopolítica solo cuenta si la cripto la confirma (el mayor de 0,5 % y 2σ de 60 min,
        # en 60 min). Recibe las captadas y las triadas por el gancho del bus; velas de la vigilancia y la caché.
        refl = optional("El reflejo", lambda: Reflejo(bus, bars=CacheBars(live=lambda c: vigil.bars.get(c)).get))
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
                bus, store, ticket_submit, ticket_pf, bars=CacheBars(live=lambda c: vigil.bars.get(c)).get))
        if entrada is not None:
            observe_bus(bus, entrada.observe)
        # Reacción: la cripto en cada apertura de bolsa (cripto_aperturas); la historia, en un hilo y una vez al día.
        react = optional("La reacción de la cripto en las aperturas", lambda: Reaccion(
            bus, bars=CacheBars(live=lambda c: vigil.bars.get(c)).get))
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
                    "local_ai": {"queue": local.q.qsize(), "done": local.done},
                    "clients": len(bus.clients), "broker": "real" if usar_real else "paper",
                    "real": {"modo": real.estado.modo} if real is not None else {"modo": "apagado"},
                    "comisiones": comisiones.activo()}

        # Velas de la gráfica de cada posición (GET /api/velas): por defecto las públicas de OKX EU del par que se operaría
        # (BTC-USDC…, sin claves, con caché y límite de peticiones); si OKX falla, las del bot: las de la vigilancia en
        # memoria (guarda semanas) y, si no llegan, las de la caché de 1 min en disco, sin pedir nada a la red. Con su
        # propia caché de meses (4 monedas × 2 meses; las lee el servidor en un hilo).
        services = Services(feeds=feeds, predictor=predictor, broker=broker, phase2=phase2, backtest=bt, openings=openings,
                            vigilancia=vigil, aprendizaje=learn, evolucion=evo, fuentes_vivas=vivas, reaccion=react,
                            reflejo=refl, fiables=fiab, entrada_noticias=entrada, decisiones=decs,
                            velas=velas_de(lambda c: vigil.bars.get(c),
                                           CacheBars(live=lambda c: vigil.bars.get(c), keep=8).get),
                            velas_okx=VelasOKX(session), real=real)
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

        extra = [m.run() for m in (learn, vivas, evo, refl, react, fiab, entrada, real)
                 if m is not None]   # cada run() atrapa sus fallos (el del modo real, apagado, no pide nada a OKX)
        try:
            await asyncio.gather(*(run_source(st, session, desk) for st in states),
                                 market.run(), agenda.run(bus, session), local.run(), report_sources(),
                                 trend.run(), feeds.run(), backtester.run(), predictor.run(), broker.run(), phase2.run(),
                                 openings.run(), vigil.run(), maintenance(store), *extra)
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
                await vigil.close()
            except Exception:
                log.exception("La vigilancia no pudo guardar sus velas al cerrar")
            await phase2.close()
            local.close()
            await runner.cleanup()
            if decs is not None:   # lo pendiente de decisiones_apertura, a la base (antes de cerrarla)
                try:
                    decs.close()
                except Exception:
                    log.exception("Las decisiones de las aperturas no pudieron guardar lo pendiente al cerrar")
            store.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
