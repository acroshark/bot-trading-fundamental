"""Servidor local: sirve la web, le envía los eventos en directo por WebSocket y expone la API de solo lectura."""
import asyncio
import json
import logging
import math
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from aiohttp import WSCloseCode, WSMsgType, web

from . import aprendizaje, comisiones, config, reaccion, reflejo, resumen_backtest
from .backtest import filter_results
from .bus import Bus, dumps_web
from .lab_vivo_lectura import ACCIONES as LAB_ACCIONES, DIA_RE as LAB_DIA_RE, ID_RE as LAB_ID_RE, LabVivoLectura
from .market import bias_of, funding_8h
from .models import now_ms
from .ranking import LIMITE_DEF as RANKING_LIMITE, LIMITE_MAX as RANKING_MAX, ORIGEN_RE, Ranking
from .regions import MARKETS, REGIONS

log = logging.getLogger("server")
LIVE_FLAG = '<script>window.RADAR_LIVE = {ws: "/events"};</script>'
MAD = ZoneInfo("Europe/Madrid")
TIER_ADJ = {"T0": 5, "T1": 2, "T2": 0, "T3": -5}   # suave: la IA local ya ve el nivel de la fuente
NEWS_MAX = 60
NEWS_KINDS = ("captured", "triaged", "lowprio", "corroborated", "srcstate", "reflejo")
STORY_MAX = 128             # ids de historia: «n…», «w-…», «a-…»
LOOPBACK = ("127.0.0.1", "::1")
# Órdenes de la web sobre las fuentes (bot/fuentes_vivas.py: accion) y valores admitidos en las rutas
FUENTE_ACCIONES = ("restaurar", "silenciar", "retirar", "activar", "a_prueba", "anadir")
MEDIOS_ORDEN = ("peso", "acierto", "casos", "ruido", "ops", "R")
APR_POR = ("nivel", "region", "idioma", "tipo", "ventana", "fase", "rol", "moneda")
HORIZONTES = (15, 60, 240, 1440)
SEMANA = re.compile(r"\d{4}-W\d{2}")
# GET /api/velas: tamaños de vela admitidos y tope de velas por respuesta (las más recientes)
VELAS_TF = {"1m": 60_000, "5m": 300_000, "15m": 900_000}
VELAS_MAX = 2000
VELAS_FUENTES = ("okx", "bot")   # okx: las públicas de OKX EU del par que se operaría (por defecto); bot: las del bot
MIN_MS = 60_000
# Mandos del modo real ({"type": "real", "accion": …}). Todos exigen la cabecera Origin de la propia web y una conexión
# desde este PC, salvo la parada (solo reduce el riesgo). El Origin frena a otras webs abiertas en el navegador, no a un
# proceso de este PC (puede poner la cabecera que quiera): lo que ninguno se salta son las otras puertas del modo real
# (okx.env con tus claves, BOT_MODO_REAL=1 al arrancar, tu frase escrita y BOT_BROKER=real), que pones tú (docs/06).
REAL_ACCIONES = ("confirmar", "parar", "limites", "detener", "reanudar", "aprobacion")


@dataclass
class Services:
    """Módulos que la API consulta. Cualquiera puede faltar (pruebas, arranque parcial)."""
    feeds: Any = None        # FeedPoller
    predictor: Any = None    # Predictor
    broker: Any = None       # PaperBroker
    phase2: Any = None       # Phase2
    backtest: Callable[[], dict | None] | None = None   # resultados del backtest (con caché)
    openings: Any = None     # OpeningEngine (motor de aperturas)
    vigilancia: Any = None   # Vigilancia (prueba en avance de las estrategias del laboratorio, sin operar)
    aprendizaje: Any = None  # Aprendizaje (medida de noticias y medios, pesos y post-mortems)
    evolucion: Any = None    # Evolucion (historial de cambios, campeón y aspirantes, propuestas e informe semanal)
    fuentes_vivas: Any = None   # FuentesVivas (estado de cada fuente y descubrimiento de medios)
    reaccion: Any = None     # Reaccion (la cripto en cada apertura de bolsa: «cripto_aperturas»)
    reflejo: Any = None      # Reflejo (¿la cripto refleja las noticias macro y geopolíticas?)
    fiables: Any = None      # Fiables (grupos de noticias que aciertan ≥ 60 % de forma constante)
    entrada_noticias: Any = None   # EntradaNoticias (entrada por noticia fiable confirmada por el precio)
    decisiones: Any = None   # Decisiones (por qué entra o no en cada apertura: tabla decisiones_apertura)
    # Velas de 1 min que el bot ya tiene: velas(moneda, desde, hasta) -> filas [t, o, h, l, c] que abren en [desde, hasta),
    # ordenadas y sin repetidas (velas_de: la vigilancia en memoria si llega hasta «desde»; si no, aprendizaje.CacheBars.get,
    # la caché de HIST_DIR/1m más la vigilancia en memoria).
    velas: Callable[[str, int, int], Any] | None = None
    # Velas públicas de OKX EU (bot/velas_okx.py: VelasOKX.respuesta), sin claves; si faltan o fallan, las del bot.
    velas_okx: Any = None
    real: Any = None         # RealBroker (bot/okx_real.py): modo real con OKX EU, APAGADO salvo que el usuario lo active
    # Ranking de estrategias (bot/ranking.py) y lectura del laboratorio vivo (bot/lab_vivo_lectura.py: solo lee sus
    # archivos, nunca importa bot.lab_vivo). Sin ellos, el servidor crea los suyos al primer GET (solo leen archivos).
    ranking: Any = None
    lab_vivo: Any = None


def _hora(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, MAD).strftime("%d-%m %H:%M")


def _dumps(o) -> str:
    """Las respuestas de la API y la foto inicial del WebSocket: nunca con NaN ni Infinity (bus.dumps_web)."""
    return dumps_web(o)


def adjusted_credibility(cred: float | None, tier: str | None, confirmations: int) -> int | None:
    """Credibilidad del triaje ajustada por el nivel de la fuente y las confirmaciones de otros medios (contrato 2.7)."""
    if cred is None:
        return None
    return int(max(0, min(100, cred + TIER_ADJ.get(tier or "", 0) + min(15, 5 * confirmations))))


def _w(x, default: float = 1.0) -> float:
    try:
        return default if x is None else float(x)
    except (TypeError, ValueError):
        return default


def news_credibility(cred: float | None, tier: str | None, confirmations: int, outlet: str | None,
                     src_weight: float = 1.0) -> int | None:
    """La de adjusted_credibility multiplicada por el peso aprendido del medio (aprendizaje.source_weight: 1 mientras
    no haya datos) y por el peso del estado de la fuente (fuentes_vivas: silenciada o retirada = 0)."""
    base = adjusted_credibility(cred, tier, confirmations)
    if base is None:
        return None
    return aprendizaje.adjusted_credibility(base, outlet, weight=aprendizaje.source_weight(outlet) * src_weight)


def _pick_stories(db, since: int, min_imp: int, bolsa: str | None) -> list[str]:
    """Las historias que devolverá /api/noticias, sin leer los cuerpos enteros: el último triaje de cada una (importancia
    y bolsas, con json_extract) y las captadas de la más nueva a la más vieja hasta tener NEWS_MAX que pasen el filtro."""
    tri: dict[str, tuple] = {}
    if min_imp > 0 or bolsa:
        bolsas = "json_extract(body, '$.patch.triage.repercusion.bolsas')" if bolsa else "NULL"
        for story, _ts, imp, bols in db.execute(
                f"SELECT story, MAX(ts), json_extract(body, '$.patch.triage.importancia'), {bolsas} FROM events "
                "WHERE kind IN ('triaged', 'lowprio') AND ts >= ? GROUP BY story", (since,)):
            tri[story] = (imp, json.loads(bols) if isinstance(bols, str) and bols.startswith("[") else [])
    region = "json_extract(body, '$.patch.region')" if bolsa else "NULL"
    out, done = [], set()
    for story, reg in db.execute(f"SELECT story, {region} FROM events WHERE kind = 'captured' AND ts >= ? "
                                 "ORDER BY ts DESC, rowid DESC", (since,)):
        if story in done:   # la primera vez que sale es su captura más reciente
            continue
        done.add(story)
        imp, listed = tri.get(story, (None, []))
        if (imp is None and min_imp > 0) or (imp is not None and imp < min_imp):
            continue   # con un mínimo de importancia solo cuentan las ya puntuadas por la IA local
        if bolsa and bolsa != REGIONS.get(reg or "", (None, None))[1] and bolsa not in listed:
            continue
        out.append(story)
        if len(out) == NEWS_MAX:
            break
    return out


def recent_news(bus: Bus, hours: float, min_imp: int, bolsa: str | None = None) -> list[dict]:
    """Noticias relevantes recientes con su triaje local, en el formato que leen las skills de OpenClaw.
    Usa su propia conexión de solo lectura: se puede (y se debe) llamar desde un hilo, fuera del bucle.
    Cada una lleva «clase» (cripto, macro o contexto, del prefiltro) y «reflejo» (el último estado del evento «reflejo»,
    o null): una macro o geopolítica que la cripto no ha confirmado sale con credibilidad_ajustada 0."""
    since = now_ms() - int(hours * 3600_000)
    with bus.store.reader() as db:
        pick = _pick_stories(db, since, min_imp, bolsa)
        if not pick:
            return []
        rows = db.execute(f"SELECT body FROM events WHERE story IN ({','.join('?' * len(pick))}) AND ts >= ? "
                          f"AND kind IN ({','.join('?' * len(NEWS_KINDS))}) ORDER BY ts, rowid",
                          (*pick, since, *NEWS_KINDS)).fetchall()
    evs = [json.loads(r[0]) for r in rows]
    by: dict[str, dict] = {}
    for e in evs:
        s, p = by.setdefault(e["story"], {}), e.get("patch", {})
        if e["kind"] == "captured":
            s.update(ts=e["ts"], hora=_hora(e["ts"]), titular=p.get("headline"), fuente=p.get("src"), nivel=p.get("tier"),
                     monedas=p.get("coins"), url=p.get("url"), region=p.get("region"), idioma=p.get("lang"),
                     fresca=p.get("fresh", True), temas_reglas=p.get("topics", []),
                     medio=p.get("outlet") or aprendizaje.outlet_from_patch(p) or None,
                     estado_fuente=p.get("srcState") or "activa", peso_fuente=_w(p.get("srcWeight")),
                     clase=p.get("clase"))
        elif e["kind"] in ("triaged", "lowprio"):
            tr = p.get("triage", {})
            s.update(importancia=tr.get("importancia"), tipo=tr.get("tipo"), sentimiento=tr.get("sentimiento"),
                     resumen=tr.get("resumen"), motivo=tr.get("motivo"), archivada=e["kind"] == "lowprio",
                     credibilidad=tr.get("credibilidad"), alcance=tr.get("alcance"), temporalidad=tr.get("temporalidad"),
                     repercusion=tr.get("repercusion"), novedad=tr.get("novedad"), temas=tr.get("temas"))
        elif e["kind"] == "reflejo":
            s["reflejo"] = p.get("reflejo")
        else:
            if e["kind"] == "corroborated":   # una confirmación de una fuente silenciada no es independiente
                s["confirmaciones"] = sum(1 for c in p.get("corrob", []) if _w((c or {}).get("srcWeight")) > 0)
            if p.get("srcState"):   # la historia sube de estado (la trae o la confirma una fuente mejor)
                s.update(estado_fuente=p["srcState"], peso_fuente=_w(p.get("srcWeight")))
    out = []
    for s in by.values():   # el filtro de importancia y bolsa ya lo hizo _pick_stories
        if not s.get("titular"):
            continue
        s["peso_medio"] = aprendizaje.source_weight(s.get("medio"))
        s["credibilidad_ajustada"] = news_credibility(s.get("credibilidad"), s.get("nivel"), s.get("confirmaciones", 0),
                                                      s.get("medio"), s.get("peso_fuente", 1.0))
        s["reflejo"] = s.get("reflejo")
        tri = {"tipo": s.get("tipo"), "temas": s.get("temas"), "repercusion": s.get("repercusion"),
               "resumen": s.get("resumen")}
        if s["credibilidad_ajustada"] is not None and not reflejo.cuenta(s.get("titular"), tri, s["reflejo"], s.get("clase")):
            s["credibilidad_ajustada"] = 0   # macro o geopolítica sin reflejo confirmado en la cripto: no cuenta
        out.append(s)
    return sorted(out, key=lambda s: -s["ts"])[:NEWS_MAX]


def ws_origins() -> set[str]:
    """Orígenes que pueden abrir /events desde un navegador: la propia web del bot (y los de BOT_WS_ORIGINS)."""
    hosts = {"127.0.0.1", "localhost", "[::1]", config.HOST}
    return {f"http://{h}:{config.PORT}" for h in hosts} | set(config.WS_ORIGINS)


def origen_web(origin: str | None, remote: str | None = None) -> bool:
    """Origen estricto de los mandos del modo real (docs/06, contrato de cableado): solo la propia web del bot,
    http://127.0.0.1:PUERTO o http://localhost:PUERTO, y la conexión desde este PC (remote en LOOPBACK: con
    BOT_HOST=0.0.0.0, un equipo de la red con el Origin falsificado no pasa). Sin cabecera Origin o con los orígenes
    extra de BOT_WS_ORIGINS, no. Ojo: el Origin solo frena a otras webs abiertas en el navegador; un proceso de este PC
    puede poner la cabecera que quiera (lo frenan las otras puertas del modo real, que pones tú: docs/06)."""
    return (remote in LOOPBACK and isinstance(origin, str)
            and origin.rstrip("/") in {f"http://127.0.0.1:{config.PORT}", f"http://localhost:{config.PORT}"})


def real_apagado() -> dict:
    """GET /api/real sin el conector construido."""
    return {"modo": "apagado", "etiqueta": "apagado", "broker": "paper", "puede_abrir": False,
            "resumen": "Modo real apagado: el bot opera en paper",
            "por_que": "El conector del modo real no está en marcha en este arranque"}


def _story(m: dict) -> str | None:
    s = m.get("story")
    return s if isinstance(s, str) and 0 < len(s) <= STORY_MAX else None


def _media(vals) -> float | None:
    v = [float(x) for x in vals if isinstance(x, (int, float)) and not isinstance(x, bool)]
    return round(sum(v) / len(v), 3) if v else None


def cripto_aperturas_resumen(msg: dict | None) -> dict:
    """Para /api/estado: cómo va la cripto en cada apertura de bolsa (del último «cripto_aperturas», nunca el índice).
    Por bolsa: estado, hora de apertura (Madrid), lo que lleva o hizo cada moneda (en curso: desde la apertura;
    terminada: a las 4 h; por abrir: la última apertura a las 4 h) y su media."""
    out = {}
    for k, m in ((msg or {}).get("markets") or {}).items():
        if not isinstance(m, dict):
            continue
        state = m.get("state")
        if state in ("en_curso", "terminada"):
            src, ret_k, rng_k, t0 = m.get("coins") or {}, "ret_pct", "range_pct", m.get("open_ms")
        else:
            last = m.get("last") or {}
            src, ret_k, rng_k, t0 = last.get("coins") or {}, "ret_4h_pct", "range_4h_pct", last.get("open_ms")
        coins = {c: {"pct": v.get(ret_k), "rango_pct": v.get(rng_k)} for c, v in src.items() if isinstance(v, dict)}
        out[k] = {"nombre": m.get("name"), "estado": state,
                  "apertura": _hora(int(t0)) if isinstance(t0, (int, float)) else None,
                  "minutos_desde_apertura": m.get("minutes_since_open"),
                  "medida": {"en_curso": "desde la apertura", "terminada": "a las 4 h"}.get(state, "última apertura, a las 4 h"),
                  "cripto": coins or None, "media_pct": _media(v["pct"] for v in coins.values())}
    return out


def state_summary(bus: Bus) -> dict:
    st = bus.state
    market = st.get("market", {})
    flow = {}
    for c, f in (market.get("flow") or {}).items():
        b = bias_of(f["ls"], funding_8h(f), f.get("pctl"))
        flow[c] = {"sesgo_minorista": b["label"], "pct_cuentas_largo": round(b["longPct"]), "ratio_ls": round(f["ls"], 2),
                   "percentil_7_dias": round(f["pctl"]) if f.get("pctl") is not None else None,
                   "funding_pct_8h": round(funding_8h(f), 4), "funding_periodo_h": f.get("funding_h"), "oi_1h_pct": round(f["oi"], 2), "compras_agresivas_pct": round(f["taker"] * 100),
                   "prima_coinbase_pct": round(f["prem"], 3) if f.get("prem") is not None else None, "extremo": b["extreme"]}
    now = now_ms()
    agenda = [{"cuando": _hora(e["t"]), "evento": e["name"], "fuente": e["src"], "impacto": e["impact"]}
              for e in (st.get("calendar") or {}).get("events", []) if e["t"] > now][:8]
    fuentes = [{"nombre": s["name"], "ok": s["ok"], "noticias": s["n"], "error": s.get("err") or None}
               for s in (st.get("sources") or {}).get("sources", [])]
    masivas = (st.get("sources_summary") or {}).get("totals")
    aperturas = cripto_aperturas_resumen(st.get("cripto_aperturas"))
    libro = {c: {"desequilibrio": o.get("imbalance"), "cvd_5m": o.get("cvd_5m"), "compras_5m": o.get("buy_ratio_5m"),
                 "tendencia": o.get("trend")} for c, o in ((st.get("orderflow") or {}).get("coins") or {}).items()}
    local = st.get("agent:local", {}).get("patch", {})
    cartera = st.get("portfolio") or {}
    return {"fase": config.PHASE, "hora_madrid": _hora(now), "precios_usdc": market.get("prices", {}), "flujo_minorista": flow,
            "libro_y_operaciones": libro, "aperturas_cripto": aperturas, "agenda_macro": agenda, "fuentes": fuentes,
            "fuentes_masivas": masivas,
            "ia_local": {"encendida": not local.get("off", True), "estado": local.get("note") or local.get("now")},
            "cartera_paper": {"capital": cartera.get("equity"), "pnl_dia": cartera.get("day_pnl"),
                              "posiciones": len(cartera.get("positions", []))} if cartera else None}


def agrupar_velas(rows, step: int) -> tuple[list[list], int | None]:
    """Velas de 1 min [t, o, h, l, c] (ordenadas) agrupadas en velas de step ms: apertura de la primera, máximo, mínimo y
    cierre de la última de cada tramo (la última puede ir a medias). Se saltan las filas con precios no válidos (un JSON
    con NaN no lo lee el navegador). Devuelve (velas, apertura de la última vela de 1 min válida o None)."""
    out: list[list] = []
    last = None
    for r in rows:
        try:
            t, o, h, lo, c = int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])
        except (TypeError, ValueError, IndexError):
            continue
        if not all(math.isfinite(x) and x > 0 for x in (o, h, lo, c)):
            continue
        h, lo = max(h, o, c), min(lo, o, c)
        b = t // step * step
        if out and out[-1][0] == b:
            v = out[-1]
            v[2], v[3], v[4] = max(v[2], h), min(v[3], lo), c
        else:
            out.append([b, o, h, lo, c])
        last = t
    return out, last


def velas_de(live, cache):
    """Fuente de velas de GET /api/velas: velas(moneda, desde, hasta) -> filas [t, o, h, l, c] de 1 min que abren en
    [desde, hasta). Si las de la vigilancia en memoria (live(moneda): array ordenado [t, o, h, l, c, …]) empiezan en
    «desde» o antes (guarda semanas: lo normal para una posición abierta), salen de ahí sin leer el disco; si no (vigilancia
    apagada, arrancando o un tramo más antiguo), de cache (CacheBars.get: la caché de 1 min en disco más las de memoria).
    Nunca pide nada a la red."""
    def get(coin: str, a: int, b: int):
        try:
            m = live(coin)
        except Exception:
            m = None
        if m is not None and getattr(m, "ndim", 0) == 2 and len(m) and m.shape[1] >= 5 and m[0, 0] <= a:
            i, j = m[:, 0].searchsorted([a, b])
            return m[i:j, :5]
        return cache(coin, a, b)
    return get


def velas_respuesta(get, coin: str, desde: int, hasta: int, tf: str) -> dict | None:
    """GET /api/velas (contrato §3): las velas que el bot ya tiene de coin entre desde y hasta, agrupadas a tf. Como mucho
    VELAS_MAX, las más recientes (recortada = true si no caben desde «desde»). None si no hay ninguna."""
    step = VELAS_TF[tf]
    a0 = desde // step * step
    a = max(a0, (hasta - 1) // step * step - (VELAS_MAX - 1) * step)
    rows, last = agrupar_velas(get(coin, a, hasta), step)
    rows = rows[-VELAS_MAX:]
    if not rows:
        return None
    return {"coin": coin, "tf": tf, "desde": rows[0][0], "hasta": hasta, "hasta_datos": last + MIN_MS,
            "recortada": a > a0, "n": len(rows), "velas": rows}


# Datos incrustados de la demostración que en directo sobran (llegan por la API): el backtest, y los ejemplos del
# ranking (rankingData) y del laboratorio vivo (labVivoData)
BT_BLOCK = re.compile(r'<script type="application/json" id="(?:bt(?:Open)?Data|impulsoData|seis2?Data|ruptura3Data'
                      r'|rankingData|labVivoData)">.*?</script>', re.S)


def make_app(bus: Bus, health, services: Services | None = None) -> web.Application:
    sv = services or Services()
    page = {"mtime": None, "html": ""}

    def live_page() -> str:
        """La web con la marca de modo en directo y sin el backtest incrustado de la demostración (llega por /api/backtest).
        Se vuelve a leer solo si el archivo cambia."""
        mt = config.DASHBOARD.stat().st_mtime
        if mt != page["mtime"]:
            html = BT_BLOCK.sub("<!--BT-->", config.DASHBOARD.read_text(encoding="utf-8"))
            page["html"], page["mtime"] = "<!doctype html>\n" + LIVE_FLAG + "\n" + html, mt
        return page["html"]

    async def index(_request: web.Request) -> web.Response:
        return web.Response(text=await asyncio.to_thread(live_page), content_type="text/html", charset="utf-8",
                            headers={"Cache-Control": "no-store"})

    def broker_call(name: str, *args):
        """Llama a un método del paper solo si existe (halt, resume y cancel llegan con el paper nuevo)."""
        fn = getattr(sv.broker, name, None)
        if not callable(fn):
            log.warning("El paper aún no admite «%s» desde la web: orden ignorada", name)
            return None
        return fn(*args)

    def order_done(area: str, accion, oid, res: dict | None) -> None:
        """Resultado de una orden de la web sobre fuentes o pesos: queda en el estado «orden_web» (la web lo enseña)."""
        res = res if isinstance(res, dict) else {"ok": False, "why": "No encontrado"}
        why = res.get("why") or res.get("error")
        bus.publish({"type": "orden_web", "ts": now_ms(), "area": area, "accion": accion,
                     "id": oid if isinstance(oid, (str, int)) and not isinstance(oid, bool) else None,
                     "ok": bool(res.get("ok", True)), "why": why})
        (log.info if res.get("ok", True) else log.warning)("Orden de la web (%s %s %s): %s", area, accion, oid,
                                                           why or "hecha")

    def source_order(m: dict) -> None:
        """{"type": "fuente", "accion": "restaurar|silenciar|retirar|activar|a_prueba|anadir", "id": "…", "motivo"}:
        cambia el estado de una fuente (fuentes_vivas.accion). El cambio queda en evolution_log con su motivo."""
        acc, fid, why = m.get("accion"), m.get("id"), m.get("motivo")
        if not sv.fuentes_vivas:
            log.warning("Orden de fuentes sin el módulo fuentes_vivas: ignorada")
        elif acc not in FUENTE_ACCIONES or not (isinstance(fid, str) and 0 < len(fid) <= 300):
            log.warning("Orden de fuentes no válida (%r, %r): ignorada", acc, fid)
        else:
            res = sv.fuentes_vivas.accion({"accion": acc, "id": fid,
                                           "motivo": " ".join(why.split())[:200] if isinstance(why, str) else None})
            order_done("fuentes", acc, fid, res)

    def weight_order(kind: str, m: dict) -> None:
        """Peso de un medio a mano ({"type": "peso_medio", "medio", "peso": número o null, "motivo"}) o deshacer un
        cambio de peso ({"type": "revertir_peso", "id"}); también con «cmd» aprendizaje_peso / aprendizaje_revertir."""
        apr = sv.aprendizaje
        if not apr:
            log.warning("Orden de pesos sin el módulo de aprendizaje: ignorada")
            return
        if kind in ("peso_medio", "aprendizaje_peso"):
            outlet, peso, why = m.get("medio") or m.get("outlet"), m.get("peso"), m.get("motivo")
            if not (isinstance(outlet, str) and 0 < len(outlet) <= 200) or isinstance(peso, bool) \
                    or not (peso is None or isinstance(peso, (int, float))):
                log.warning("Orden de peso no válida (%r, %r): ignorada", outlet, peso)
                return
            motivo = " ".join(why.split())[:300] if isinstance(why, str) and why.strip() else "Fijado a mano desde la web"
            order_done("aprendizaje", "peso", outlet, apr.fijar_peso(outlet, peso, motivo))
        else:
            cid = m.get("id")
            if isinstance(cid, bool) or not (isinstance(cid, int) or (isinstance(cid, str) and cid.isdigit())):
                log.warning("Orden de revertir peso sin id numérico (%r): ignorada", cid)
                return
            # «n.º N» en el motivo: la evolución enlaza la reversión con el cambio deshecho (revierte / revertida_por)
            order_done("aprendizaje", "revertir", int(cid),
                       apr.revertir(int(cid), f"Revertido el cambio n.º {int(cid)} desde la web"))

    tasks: set[asyncio.Task] = set()

    async def discard_and_log(m: dict) -> None:
        """Descarta una propuesta (Evolucion.atender) y deja la decisión en evolution_log: lo descartado queda
        bloqueado (no se vuelve a proponer sin datos posteriores)."""
        evo = sv.evolucion
        try:
            res = await evo.atender(m)
            if not res.get("ok"):
                return
            pid, why = m["id"], m.get("motivo") if isinstance(m.get("motivo"), str) else ""
            props = (await evo.api_propuestas("descartada", 200)).get("propuestas") or []
            prop = next((x for x in props if x.get("id") == pid), {})
            title = (prop.get("cuerpo") or {}).get("titulo") or pid
            evo.registrar("propuesta_descartada", prop.get("sujeto") or pid, {"propuesta": pid, "estado": "pendiente"},
                          {"propuesta": pid, "estado": "descartada"},
                          f"Propuesta descartada por ti desde la web: «{title}»" + (f" ({why[:200]})" if why.strip() else ""),
                          {"propuesta": pid, "tipo": prop.get("tipo"), "clave": prop.get("clave")}, "web")
        except Exception:
            log.exception("No pude descartar la propuesta %s o apuntarlo en el historial", m.get("id"))

    def evolution_order(m: dict) -> None:
        """{"type": "evolucion", "accion": "aplicar|descartar|revertir|evaluar|modo", "id", "motivo", "modo"}: la
        atiende la evolución en una tarea (su resultado sale en «evolucion».ultima_orden). Aplicar y revertir quedan en
        evolution_log por la propia evolución; descartar, aquí."""
        evo = sv.evolucion
        if not evo:
            log.warning("Orden de la evolución sin el módulo: ignorada")
        elif m.get("accion") == "descartar" and isinstance(m.get("id"), str) and 0 < len(m["id"]) <= 128:
            t = asyncio.get_running_loop().create_task(discard_and_log(m))
            tasks.add(t)
            t.add_done_callback(tasks.discard)
        elif not evo.orden_web(m):   # valida la acción y la atiende en una tarea
            log.warning("Orden de la evolución no válida: %s", str(m)[:200])

    def commission_order(m: dict) -> None:
        """{"type": "comisiones", "perfil": "okx_eu_con_xperps|okx_eu_sin_xperps"}: perfil de comisiones del paper
        (PaperBroker.set_comisiones, guardado). Respuesta en «orden_web» (area comisiones)."""
        perfil = m.get("perfil")
        if not isinstance(perfil, str) or perfil not in comisiones.PERFILES:
            return order_done("comisiones", "perfil", None, {"ok": False, "why": "Perfil de comisiones desconocido: usa "
                                                                               + " o ".join(comisiones.PERFILES)})
        if sv.broker is None or not callable(getattr(sv.broker, "set_comisiones", None)):
            return order_done("comisiones", "perfil", perfil, {"ok": False, "why": "El paper no está en marcha"})
        res = sv.broker.set_comisiones(perfil)
        order_done("comisiones", "perfil", perfil, res if isinstance(res, dict) else {"ok": bool(res)})

    def real_story(story: str | None) -> bool:
        """¿Es una story del modo real (esperando aprobación, en curso o con posición en OKX)?"""
        r = sv.real
        return bool(story and r is not None and any(story in (getattr(r, k, None) or {})
                                                    for k in ("pending", "en_curso", "positions")))

    def real_order(m: dict, origin: str | None, remote: str | None) -> None:
        """{"type": "real", "accion": …}: mandos del panel «Cuenta real». La parada se admite sin Origin (solo reduce el
        riesgo); el resto exige el de la propia web y una conexión desde este PC. Respuesta en «orden_web» (area real);
        nunca toca okx.env."""
        acc, r = m.get("accion"), sv.real
        if acc not in REAL_ACCIONES:
            return order_done("real", None, None, {"ok": False, "why": "Acción del modo real desconocida"})
        if r is None:
            return order_done("real", acc, None, {"ok": False, "why": "El conector del modo real no está en marcha"})
        if acc != "parar" and not origen_web(origin, remote):
            log.warning("Mando del modo real «%s» rechazado: sin el origen de la propia web (%s desde %s)", acc, origin,
                        remote)
            return order_done("real", acc, None, {"ok": False, "why": "Solo desde la propia web del bot (cabecera Origin)"})
        if acc == "confirmar":
            frase = m.get("frase")
            res = r.confirmar(frase if isinstance(frase, str) else "")
        elif acc == "parar":
            cerrar = m.get("cerrar") is True   # solo el true de JSON cierra posiciones (la cadena "true", no)
            hecho = r.parada_emergencia("Parada de emergencia desde la web", cerrar)
            res = {"ok": True, "why": None if hecho else "La parada ya estaba pedida"}
        elif acc == "limites":
            lim = m.get("limites")
            res = r.set_limites(lim) if isinstance(lim, dict) else {"ok": False, "why": "Faltan los límites"}
        elif acc in ("detener", "reanudar"):
            hecho = r.halt("Detenido desde la web") if acc == "detener" else r.resume("Reanudado desde la web")
            res = {"ok": True, "why": None if hecho else ("Ya estaba detenido" if acc == "detener" else "No estaba detenido")}
        else:   # aprobacion
            res = {"ok": bool(r.set_mode(m.get("mode"))) if m.get("mode") in ("auto", "veto", "manual") else False}
            if not res["ok"]:
                res["why"] = "Aprobación no válida (la automática solo en la demo de OKX)"
        order_done("real", acc, None, {k: res.get(k) for k in ("ok", "why")} if isinstance(res, dict) else {"ok": False})
        try:
            r.publish_cuenta(force=True)
        except Exception:
            log.exception("No pude publicar la cuenta real tras un mando")

    por_defecto: dict[str, Any] = {}

    def labv() -> LabVivoLectura:
        """La lectura del laboratorio vivo (la de __main__ o, sin ella, una propia que solo lee archivos)."""
        if sv.lab_vivo is not None:
            return sv.lab_vivo
        if "lab" not in por_defecto:
            por_defecto["lab"] = LabVivoLectura(bus)
        return por_defecto["lab"]

    def rk() -> Ranking:
        """El ranking de estrategias (el de __main__ o, sin él, uno propio con lo que haya en el bus)."""
        if sv.ranking is not None:
            return sv.ranking
        if "ranking" not in por_defecto:
            res = getattr(sv.broker, "results", None)
            por_defecto["ranking"] = Ranking(bus, get_resultados=res if callable(res) else None, lab=labv())
        return por_defecto["ranking"]

    def lab_order(m: dict, origin: str | None, remote: str | None) -> None:
        """{"type": "lab_vivo", "accion": "pausar|reanudar"} (docs/07, 14.3): crea o borra el archivo pausa_manual del
        laboratorio vivo. Solo con el origen estricto de la propia web; el resultado sale en «orden_web» (area
        lab_vivo) y el mensaje «lab_vivo» se vuelve a publicar al momento."""
        acc = m.get("accion")
        if acc not in LAB_ACCIONES:
            return order_done("lab_vivo", None, None, {"ok": False, "why": "Acción del laboratorio vivo desconocida "
                                                                           "(pausar o reanudar)"})
        if not origen_web(origin, remote):
            log.warning("Mando del laboratorio vivo «%s» rechazado: sin el origen de la propia web (%s desde %s)", acc,
                        origin, remote)
            return order_done("lab_vivo", acc, None, {"ok": False, "why": "Solo desde la propia web del bot (cabecera Origin)"})
        lab = labv()
        res = lab.mando(acc)
        order_done("lab_vivo", acc, None, res)
        try:
            lab.publicar(forzar=True)
        except Exception:
            log.exception("No pude publicar el estado del laboratorio vivo tras un mando")

    def real_story_order(kind: str, story: str, m: dict, origin: str | None, remote: str | None) -> None:
        """decision, close y cancel de una story del modo real: van al RealBroker y solo desde la propia web."""
        if not origen_web(origin, remote):
            log.warning("Orden «%s» sobre %s (modo real) rechazada: sin el origen de la propia web (%s desde %s)", kind,
                        story, origin, remote)
            return order_done("real", kind, story, {"ok": False, "why": "Solo desde la propia web del bot (cabecera Origin)"})
        r = sv.real
        if kind == "decision":
            ok = m.get("ok")
            if not isinstance(ok, bool):
                log.warning("Decisión de la web sobre %s (modo real) sin ok booleano (%r): ignorada", story, ok)
                return
            done = r.decide(story, ok, "Aprobada por ti" if ok else "Rechazada por ti")
        elif kind == "close":
            done = r.close(story, "Cierre manual desde la web")
        else:
            done = r.cancel(story, "Cancelada por ti desde la web")
        order_done("real", kind, story, {"ok": bool(done), "why": None if done else "Nada que hacer"})

    def on_ws_message(data: str, origin: str | None = None, remote: str | None = None) -> None:
        """Órdenes de la web: sobre el paper (aprobar o rechazar, cerrar una posición, cambiar el modo de aprobación,
        detener o reanudar la ejecución, cancelar una orden pendiente o programada y el perfil de comisiones), sobre el
        modo real (real; y decision, close y cancel de sus stories, con el origen estricto), sobre las fuentes (fuente),
        la evolución (evolucion: aplicar, descartar o revertir propuestas, evaluar y modo), los pesos de los medios y el
        laboratorio vivo (lab_vivo: pausar o reanudar, con el origen estricto).
        origin: la cabecera Origin con la que se abrió el WebSocket (None si no la traía); remote: la IP de la conexión."""
        try:
            m = json.loads(data)
        except ValueError:
            return
        if not isinstance(m, dict):
            return
        kind = m.get("type") or m.get("cmd")
        if kind == "fuente":
            return source_order(m)
        if kind == "evolucion":
            return evolution_order(m)
        if kind in ("peso_medio", "revertir_peso", "aprendizaje_peso", "aprendizaje_revertir"):
            return weight_order(kind, m)
        if kind == "comisiones":
            return commission_order(m)
        if kind == "real":
            return real_order(m, origin, remote)
        if kind == "lab_vivo":
            return lab_order(m, origin, remote)
        story = _story(m)
        if kind in ("decision", "close", "cancel") and real_story(story):
            return real_story_order(kind, story, m, origin, remote)
        if not sv.broker:
            return
        if kind == "decision" and story:
            ok = m.get("ok")
            if not isinstance(ok, bool):   # solo true o false de JSON: la cadena "false" no aprueba nada
                log.warning("Decisión de la web sobre %s sin ok booleano (%r): ignorada", story, ok)
                return
            done = sv.broker.decide(story, ok, "Aprobada por ti" if ok else "Rechazada por ti")
            log.info("Decisión de la web sobre %s: %s (%s)", story, "sí" if ok else "no", "aplicada" if done else "nada pendiente")
        elif kind == "close" and story:
            sv.broker.close(story, "Cierre manual desde la web")
        elif kind == "mode" and m.get("mode") in ("auto", "veto", "manual"):
            sv.broker.set_mode(m["mode"])
        elif kind == "halt":
            broker_call("halt", "Detenida desde la web")
        elif kind == "resume":
            broker_call("resume", "Reanudada desde la web")
        elif kind == "cancel" and story:
            broker_call("cancel", story, "Cancelada por ti desde la web")
        else:
            log.warning("Mensaje de la web no reconocido: %s", str(data)[:200])

    def origin_ok(request: web.Request) -> bool:
        """Solo la propia web del bot. Sin cabecera Origin (no es un navegador) solo se admite desde este PC."""
        origin = request.headers.get("Origin")
        if origin is None:
            return request.remote in LOOPBACK
        return origin.rstrip("/") in ws_origins()

    open_ws: set[web.WebSocketResponse] = set()

    async def close_ws(_app: web.Application) -> None:
        """Al apagar, cierra las webs conectadas: si no, el cierre espera hasta 60 s a que se vayan solas."""
        for w in list(open_ws):
            await w.close(code=WSCloseCode.GOING_AWAY, message=b"apagando")

    async def events(request: web.Request) -> web.WebSocketResponse:
        # Los WebSocket no tienen CORS: sin esto, cualquier página abierta en el navegador podría mandar órdenes.
        if not origin_ok(request):
            log.warning("WebSocket rechazado: origen %s desde %s", request.headers.get("Origin"), request.remote)
            raise web.HTTPForbidden(text="origen no permitido")
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        open_ws.add(ws)
        q = bus.subscribe()
        snap = bus.snapshot()   # sin await entre suscribirse y la foto: no se pierde ni se duplica nada
        log.info("Web conectada (%d clientes)", len(bus.clients))

        async def pump():
            while True:
                first = await q.get()
                batch, slow = [], first is None   # None: el bus la ha echado por lenta
                if not slow:
                    batch.append(first)
                while not slow and not q.empty() and len(batch) < 200:
                    m = q.get_nowait()
                    if m is None:
                        slow = True
                    else:
                        batch.append(m)
                if batch:
                    await ws.send_str(batch[0] if len(batch) == 1 else "[" + ",".join(batch) + "]")
                if slow:   # al cerrar, la web se reconecta sola y recibe una foto nueva
                    log.warning("Web lenta: se cierra su conexión para que vuelva con el historial")
                    await ws.close(code=WSCloseCode.TRY_AGAIN_LATER, message=b"web lenta")
                    return

        sender = None
        try:
            await ws.send_str(_dumps(snap))   # sin NaN: la web parsea la foto entera de una vez
            sender = asyncio.create_task(pump())
            origin, remote = request.headers.get("Origin"), request.remote
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        on_ws_message(msg.data, origin, remote)
                    except Exception:
                        log.exception("Mensaje de la web no procesado")
        finally:
            bus.unsubscribe(q)
            open_ws.discard(ws)
            if sender:
                sender.cancel()
            log.info("Web desconectada")
        return ws

    async def health_route(_request: web.Request) -> web.Response:
        return web.json_response(health(), dumps=_dumps)

    # Solo lectura, para la web, las skills del OpenClaw de trading y otras herramientas locales.
    def j(o) -> web.Response:
        return web.json_response(o, dumps=_dumps)

    async def api_state(_request: web.Request) -> web.Response:
        return j(state_summary(bus))

    async def api_news(request: web.Request) -> web.Response:
        try:
            hours = max(0.0, min(48.0, float(request.query.get("horas", "3"))))
            min_imp = int(request.query.get("min", "0"))
        except ValueError:
            raise web.HTTPBadRequest(text="horas y min deben ser números")
        bolsa = request.query.get("bolsa")
        if bolsa and bolsa not in MARKETS:
            raise web.HTTPBadRequest(text=f"bolsa desconocida; usa una de: {', '.join(MARKETS)}")
        return j(await asyncio.to_thread(recent_news, bus, hours, min_imp, bolsa))   # en un hilo: 48 h son miles de filas

    async def api_sources(request: web.Request) -> web.Response:
        """Feeds con su estado de ciclo de vida (estado y peso, de fuentes_vivas); ?estado= filtra por él."""
        rows = sv.feeds.api_list(request.query.get("region")) if sv.feeds else []
        estado = request.query.get("estado")
        return j([r for r in rows if r.get("estado", "activa") == estado] if estado else rows)

    def q_int(request: web.Request, name: str, default: int | None, lo: int, hi: int) -> int | None:
        raw = request.query.get(name)
        if raw in (None, ""):
            return default
        try:
            return max(lo, min(hi, int(raw)))
        except ValueError:
            raise web.HTTPBadRequest(text=f"{name} debe ser un número entero")

    async def safe(coro, default):
        """Parte opcional de una respuesta: si falla, la respuesta sale igual (sin esa parte)."""
        try:
            return await coro
        except Exception:
            log.exception("API: una parte de la respuesta falló")
            return default

    # ---- fuentes vivas (estado de cada fuente y descubrimiento)

    async def api_fv(request: web.Request) -> web.Response:
        """Estado «fuentes_vivas» con la lista filtrada (?region=&estado=&tipo=feed|medio&limite=) y los candidatos."""
        fv = sv.fuentes_vivas
        if not fv:
            return j(bus.state.get("fuentes_vivas") or {"type": "fuentes_vivas", "estado": "apagado", "lista": [],
                                                        "candidatos": []})
        q = request.query
        tipo = q.get("tipo") or None
        if tipo not in (None, "feed", "medio"):
            raise web.HTTPBadRequest(text="tipo debe ser feed o medio")
        out = {**fv.estado(), "ts": now_ms()}
        out["lista"] = fv.lista(q.get("region") or None, q.get("estado") or None, tipo, q_int(request, "limite", 300, 1, 10_000))
        out["candidatos"] = fv.candidatos(50)
        return j(out)

    async def api_fv_lista(request: web.Request) -> web.Response:
        q = request.query
        tipo = q.get("tipo") or None
        if tipo not in (None, "feed", "medio"):
            raise web.HTTPBadRequest(text="tipo debe ser feed o medio")
        if not sv.fuentes_vivas:
            return j([])
        return j(sv.fuentes_vivas.lista(q.get("region") or None, q.get("estado") or None, tipo,
                                        q_int(request, "limite", 10_000, 1, 10_000)))

    async def api_fv_candidatos(request: web.Request) -> web.Response:
        return j(sv.fuentes_vivas.candidatos(q_int(request, "limite", 200, 1, 1000)) if sv.fuentes_vivas else [])

    async def api_fv_historial(request: web.Request) -> web.Response:
        fid = request.query.get("id") or None
        if fid is not None and len(fid) > 300:
            raise web.HTTPBadRequest(text="id demasiado largo")
        return j(sv.fuentes_vivas.historial(q_int(request, "n", 200, 1, 2000), fid) if sv.fuentes_vivas else [])

    # ---- aprendizaje (medios, noticias que funcionaron y fallaron, post-mortems)

    def apr_or_404():
        if not sv.aprendizaje:
            raise web.HTTPNotFound(text="El aprendizaje no está en marcha (BOT_APRENDIZAJE=0 o no arrancó)")
        return sv.aprendizaje

    async def api_apr(_request: web.Request) -> web.Response:
        """El estado «aprendizaje» con el ranking de medios, los post-mortems, los cambios de peso y las noticias que
        mejor y peor funcionaron (a 4 h)."""
        apr = sv.aprendizaje
        if not apr:
            return j(bus.state.get("aprendizaje") or {"type": "aprendizaje", "estado": "apagado", "medios": [],
                                                      "postmortems": [], "cambios": [], "noticias": None})
        out = await apr.view()
        out["noticias"] = {"mejores": await safe(apr.noticias("mejores"), []),
                           "peores": await safe(apr.noticias("peores"), [])}
        return j(out)

    async def api_apr_medios(request: web.Request) -> web.Response:
        q, apr = request.query, apr_or_404()
        orden = q.get("orden") or "peso"
        if orden not in MEDIOS_ORDEN:
            raise web.HTTPBadRequest(text=f"orden debe ser uno de: {', '.join(MEDIOS_ORDEN)}")
        try:
            min_casos = max(0.0, float(q.get("min_casos") or 0))
        except ValueError:
            raise web.HTTPBadRequest(text="min_casos debe ser un número")
        return j(apr.medios(orden, q_int(request, "limit", 100, 1, 1000), q.get("veredicto") or None, min_casos,
                            (q.get("q") or "")[:100] or None))

    async def api_apr_medio(request: web.Request) -> web.Response:
        outlet = request.query.get("id") or request.query.get("outlet") or ""
        if not 0 < len(outlet) <= 200:
            raise web.HTTPBadRequest(text="falta el medio (?id=dominio)")
        m = apr_or_404().medio(outlet)
        if m is None:
            raise web.HTTPNotFound(text="Sin datos de ese medio")
        return j(m)

    async def api_apr_noticias(request: web.Request) -> web.Response:
        q, apr = request.query, apr_or_404()
        cuales = q.get("cuales") or "mejores"
        horizonte = q_int(request, "horizonte", 240, 1, 1440)
        if cuales not in ("mejores", "peores") or horizonte not in HORIZONTES:
            raise web.HTTPBadRequest(text="cuales: mejores|peores; horizonte: 15|60|240|1440")
        return j(await apr.noticias(cuales, horizonte, q_int(request, "dias", 30, 1, 400), q_int(request, "limit", 20, 1, 200),
                                    q.get("solo_senal") != "0"))

    async def api_apr_estadisticas(request: web.Request) -> web.Response:
        por = request.query.get("por") or "nivel"
        if por not in APR_POR:
            raise web.HTTPBadRequest(text=f"por debe ser uno de: {', '.join(APR_POR)}")
        return j(apr_or_404().estadisticas(por))

    async def api_apr_postmortems(request: web.Request) -> web.Response:
        origen = request.query.get("origen") or None
        if origen not in (None, "paper", "vigilancia"):
            raise web.HTTPBadRequest(text="origen debe ser paper o vigilancia")
        return j(await apr_or_404().postmortems(q_int(request, "limit", 50, 1, 500), origen))

    async def api_apr_cambios(request: web.Request) -> web.Response:
        outlet = request.query.get("medio") or request.query.get("outlet") or None
        return j(await apr_or_404().cambios(q_int(request, "limit", 100, 1, 1000), outlet[:200] if outlet else None))

    # ---- evolución (historial, campeón y aspirantes, propuestas, informe semanal)

    def evo_or_404():
        if not sv.evolucion:
            raise web.HTTPNotFound(text="La evolución no está en marcha (BOT_EVOLUCION=0 o no arrancó)")
        return sv.evolucion

    async def api_evo(_request: web.Request) -> web.Response:
        """El estado «evolucion» (campeón, aspirantes, propuestas, últimos cambios) con el último informe semanal entero."""
        evo = sv.evolucion
        if not evo:
            return j(bus.state.get("evolucion") or {"type": "evolucion", "estado": "apagada", "aspirantes": [],
                                                    "propuestas": [], "cambios": [], "informe_semanal": None})
        out = dict(await evo.api_vista())
        inf = await safe(evo.api_informes(None, 1), {"informes": []})
        out["informe_semanal"] = (inf.get("informes") or [None])[0]
        return j(out)

    async def api_evo_historial(request: web.Request) -> web.Response:
        q = request.query
        tipo = q.get("tipo") or None
        return j(await evo_or_404().api_historial(tipo[:500] if tipo else None, (q.get("sujeto") or "")[:300] or None,
                                                  (q.get("origen") or "")[:60] or None,
                                                  q_int(request, "desde", None, 0, 10 ** 14),
                                                  q_int(request, "hasta", None, 0, 10 ** 14),
                                                  q_int(request, "limite", 200, 1, 2000)))

    async def api_evo_informes(request: web.Request) -> web.Response:
        semana = request.query.get("semana") or None
        if semana and not SEMANA.fullmatch(semana):
            raise web.HTTPBadRequest(text="semana con el formato AAAA-Wss (p. ej. 2026-W44)")
        return j(await evo_or_404().api_informes(semana, q_int(request, "limite", 12, 1, 200)))

    async def api_evo_propuestas(request: web.Request) -> web.Response:
        return j(await evo_or_404().api_propuestas(request.query.get("estado") or None,
                                                   q_int(request, "limite", 100, 1, 1000)))

    async def api_trend(_request: web.Request) -> web.Response:
        return j({"trend": bus.state.get("trend"), "orderflow": bus.state.get("orderflow")})

    async def api_predictions(_request: web.Request) -> web.Response:
        return j(sv.predictor.view() if sv.predictor else bus.state.get("predictions") or {"type": "predictions", "items": []})

    async def api_predictions_done(request: web.Request) -> web.Response:
        raw = request.query.get("n", "50")
        n = max(1, min(500, int(raw))) if raw.isdigit() else 50
        return j(sv.predictor.evaluated(n) if sv.predictor else [])

    async def api_backtest(request: web.Request) -> web.Response:
        q = request.query
        res = await asyncio.to_thread(sv.backtest) if sv.backtest else None   # relee el archivo (varios MB) solo si cambió
        return j(filter_results(res, q.get("family"), q.get("verdict"), q.get("id"), q.get("curvas") != "0"))

    async def api_results(_request: web.Request) -> web.Response:
        return j(sv.broker.results() if sv.broker else {"start_equity": config.PAPER_EQUITY, "equity": config.PAPER_EQUITY,
                                                          "trades": [], "by_strategy": [], "by_window": [], "by_coin": [],
                                                          "equity_curve": [], "daily": [], "comisiones_total": 0.0,
                                                          "funding_total": 0.0, "costes_total": 0.0, "bruto": 0.0,
                                                          "neto": 0.0, "costes_estimados": 0,
                                                          "comisiones": comisiones.vista()})

    async def api_portfolio(_request: web.Request) -> web.Response:
        return j(sv.broker.portfolio() if sv.broker else bus.state.get("portfolio"))

    async def api_phase2(_request: web.Request) -> web.Response:
        return j(sv.phase2.view() if sv.phase2 else {"claude": False, "estado": "apagada"})

    async def api_openings(_request: web.Request) -> web.Response:
        return j(sv.openings.view() if sv.openings else bus.state.get("openings") or {"type": "openings", "windows": []})

    async def api_cripto_aperturas(_request: web.Request) -> web.Response:
        """La cripto en cada apertura de bolsa (contrato 2.14): el último «cripto_aperturas» o «apagado»."""
        if sv.reaccion:
            return j(sv.reaccion.view())
        return j(bus.state.get("cripto_aperturas") or reaccion.apagado())

    async def api_vigilancia(_request: web.Request) -> web.Response:
        if sv.vigilancia:
            return j(sv.vigilancia.view())
        return j(bus.state.get("vigilancia") or {"type": "vigilancia", "estado": "apagada", "estrategias": []})

    bt_open = {"mtime": None, "res": None}

    def load_bt_open() -> dict | None:
        path = config.BACKTEST_DIR / "aperturas.json"
        try:
            mt = path.stat().st_mtime
        except OSError:
            return None
        if mt != bt_open["mtime"]:
            bt_open["res"], bt_open["mtime"] = json.loads(path.read_text(encoding="utf-8")), mt
        return bt_open["res"]

    async def api_bt_openings(_request: web.Request) -> web.Response:
        res = await asyncio.to_thread(load_bt_open)
        if res is None:
            raise web.HTTPNotFound(text="Aún no hay backtest de aperturas: python -m bot.backtest_aperturas")
        return j(res)

    async def api_bt_impulso(_request: web.Request) -> web.Response:
        """La estrategia del motor (E2, variante de 7 días), las de «6 diarias», «6 diarias v2» y «Ruptura V3» y el
        resumen del laboratorio."""
        res = await asyncio.to_thread(resumen_backtest.load)
        if all(res.get(k) is None for k in ("impulso", "lab", "seis", "seis2", "ruptura3")):
            raise web.HTTPNotFound(text="Aún no hay backtest del motor: python -m bot.laboratorio y controles/compara_impulso.py")
        return j(res)

    async def api_decisiones(request: web.Request) -> web.Response:
        """Por qué entra o no en cada apertura (contrato 2.17): {rows, n, desde}, la más reciente primero; por defecto los
        3 últimos días y como mucho 2.000 filas. 404 si el módulo no está en marcha (entonces no se guardan)."""
        if not sv.decisiones:
            raise web.HTTPNotFound(text="Las decisiones de las aperturas no se guardan: el módulo no arrancó")
        q = request.query
        vid, coin = q.get("ventana_id") or None, q.get("coin") or None
        if (vid and len(vid) > STORY_MAX + 16) or (coin and len(coin) > 16):
            raise web.HTTPBadRequest(text="ventana_id o coin demasiado largos")
        desde = q_int(request, "desde", None, 0, 10 ** 14)
        limite = q_int(request, "limite", None, 1, 2000)
        return j(await asyncio.to_thread(sv.decisiones.consulta, desde, vid, coin, limite))

    async def api_fiables(_request: web.Request) -> web.Response:
        """El último cálculo de las noticias fiables (contrato 2.15); 404 si aún no hay ninguno."""
        res = sv.fiables.view() if sv.fiables else bus.state.get("noticias_fiables")
        if not res:
            raise web.HTTPNotFound(text="Aún no hay cálculo de noticias fiables (se hace al arrancar y cada hora)")
        return j(res)

    velas_lock = threading.Lock()   # la caché de velas guarda meses leídos en memoria: una lectura a la vez

    def velas_hilo(coin: str, desde: int, hasta: int, tf: str) -> dict | None:
        with velas_lock:
            return velas_respuesta(sv.velas, coin, desde, hasta, tf)

    async def api_velas(request: web.Request) -> web.Response:
        """Velas de una moneda para la gráfica de cada posición (contrato §3): ?coin=BTC&desde=<ms>&tf=1m|5m|15m
        (&hasta=<ms>, por defecto ahora)&fuente=okx|bot. Con fuente okx (por defecto), las públicas de OKX EU del par que
        se operaría (BTC-USDC…, sin claves; caché y límite de peticiones en VelasOKX); si OKX no responde o no tiene
        velas, caen a las del bot y lo dicen (fuente «bot», pedida «okx» y aviso). Con fuente bot, solo las que el bot ya
        tiene (caché de 1 min de Binance y la vigilancia en memoria), sin red. Como mucho VELAS_MAX, las más recientes.
        400 con un parámetro no válido; 404 sin ninguna vela en ese tramo."""
        q = request.query
        coin = (q.get("coin") or "").upper()
        if coin not in config.COINS:
            raise web.HTTPBadRequest(text=f"coin debe ser una de: {', '.join(config.COINS)}")
        tf = q.get("tf") or "5m"
        if tf not in VELAS_TF:
            raise web.HTTPBadRequest(text=f"tf debe ser uno de: {', '.join(VELAS_TF)}")
        fuente = (q.get("fuente") or "okx").lower()
        if fuente not in VELAS_FUENTES:
            raise web.HTTPBadRequest(text=f"fuente debe ser una de: {', '.join(VELAS_FUENTES)}")
        hasta = q_int(request, "hasta", now_ms(), 1, 10 ** 14)
        desde = q_int(request, "desde", hasta - 3 * 3600_000, 0, 10 ** 14)
        if desde >= hasta:
            raise web.HTTPBadRequest(text="desde debe ser anterior a hasta")
        aviso = None
        if fuente == "okx":
            if sv.velas_okx is None:
                aviso = "Las velas de OKX EU no están enganchadas en este arranque"
            else:
                try:
                    res = await sv.velas_okx.respuesta(coin, desde, hasta, tf, VELAS_MAX)
                except Exception as e:
                    log.warning("Velas de OKX EU de %s: %s; se usan las del bot", coin, e)
                    res, aviso = None, f"OKX EU no respondió ({str(e)[:160]})"
                if res is not None:
                    return j(res)
                aviso = aviso or f"OKX EU no tiene velas de {coin}-{config.QUOTE} en ese tramo"
        if not sv.velas:
            raise web.HTTPNotFound(text="Las velas no están disponibles: el bot en marcha no tiene la caché enganchada"
                                        + (f" ({aviso})" if aviso else ""))
        res = await asyncio.to_thread(velas_hilo, coin, desde, hasta, tf)
        if res is None:
            raise web.HTTPNotFound(text=f"Sin velas de {coin} en la caché del bot en ese tramo"
                                        + (f" ({aviso})" if aviso else ""))
        res.update(fuente="bot", mercado="Binance (al contado)", instId=f"{coin}USDT",
                   nota=f"velas del bot (Binance, {coin}USDT, 1 min)")
        if fuente == "okx":
            res.update(pedida="okx", aviso=aviso + ": velas del bot")
        return j(res)

    # ---- ranking de estrategias (docs/04, 2.20) y laboratorio vivo (docs/07, 14.1)

    def q_bool(request: web.Request, name: str) -> bool | None:
        raw = (request.query.get(name) or "").strip().lower()
        if raw == "":
            return None
        if raw in ("1", "true", "si", "sí"):
            return True
        if raw in ("0", "false", "no"):
            return False
        raise web.HTTPBadRequest(text=f"{name} debe ser 1 o 0")

    async def api_ranking(request: web.Request) -> web.Response:
        """El ranking de estrategias: ?origen=a,b&cumple=1|0&min_op_dia=&limite=&fragil=1|0&todas=1. Los puestos son los
        del ranking entero (un filtro no los cambia). 400 con un parámetro no válido (limite fuera de 1-5.000 se
        recorta). Lleva ETag (Ranking.firma, sin las horas de cálculo): con If-None-Match igual, 304 sin cuerpo."""
        q = request.query
        origen = (q.get("origen") or "").strip() or None
        if origen and not all(ORIGEN_RE.fullmatch(o) for o in origen.split(",")):
            raise web.HTTPBadRequest(text="origen: uno o varios (separados por comas) de letras minúsculas, cifras y _")
        min_op = None
        if (q.get("min_op_dia") or "").strip():
            try:
                min_op = float(q["min_op_dia"].replace(",", "."))
            except ValueError:
                raise web.HTTPBadRequest(text="min_op_dia debe ser un número")
            if not (math.isfinite(min_op) and min_op >= 0):
                raise web.HTTPBadRequest(text="min_op_dia debe ser un número ≥ 0")
        cumple, fragil, todas = q_bool(request, "cumple"), q_bool(request, "fragil"), q_bool(request, "todas")
        limite = q_int(request, "limite", RANKING_LIMITE, 1, RANKING_MAX)
        r = rk()
        foto = r.foto_vivo()   # en el bucle: el paper y el bus no se leen desde un hilo

        def consulta() -> tuple[dict, str]:
            out = r.consulta(foto, origen, cumple, min_op, limite, fragil, bool(todas))
            return out, Ranking.firma(out)

        out, firma = await asyncio.to_thread(consulta)
        etag = f'"{firma}"'
        cab = {"ETag": etag, "Cache-Control": "no-cache"}
        if etag in [x.strip() for x in (request.headers.get("If-None-Match") or "").split(",")]:
            return web.Response(status=304, headers=cab)   # la web ya lo tiene: no vuelve a viajar
        txt = await asyncio.to_thread(_dumps, out)   # en un hilo también: la tabla entera pesa ~2 MB
        return web.Response(text=txt, content_type="application/json", headers=cab)

    async def api_lab_vivo(_request: web.Request) -> web.Response:
        """estado.json más mejores (hasta 20) y el diario de hoy; «apagado» sin archivo y «parado» con el latido viejo."""
        return j(await asyncio.to_thread(labv().vista))

    async def api_lab_hipotesis(request: web.Request) -> web.Response:
        """?id=h-… → hipotesis/<id>.json (404 si no existe); sin id, {hipotesis} filtradas por estado y familia."""
        q = request.query
        hid = q.get("id")
        if hid is not None:
            if not LAB_ID_RE.fullmatch(hid):
                raise web.HTTPBadRequest(text="id de hipótesis no válido (h-AAAAMMDD-HHMM-nombre)")
            d = await asyncio.to_thread(labv().hipotesis_detalle, hid)
            if d is None:
                raise web.HTTPNotFound(text="No hay esa hipótesis en el laboratorio vivo")
            return j(d)
        estado, familia = q.get("estado") or None, q.get("familia") or None
        for k, v in (("estado", estado), ("familia", familia)):
            if v is not None and not re.fullmatch(r"[a-z0-9_]{1,60}", v):
                raise web.HTTPBadRequest(text=f"{k} no válido")
        return j(await asyncio.to_thread(labv().hipotesis, estado, familia, q_int(request, "limite", 100, 1, 1000)))

    async def api_lab_diario(request: web.Request) -> web.Response:
        dia = request.query.get("dia") or None
        if dia is not None and not LAB_DIA_RE.fullmatch(dia):
            raise web.HTTPBadRequest(text="dia con el formato AAAA-MM-DD")
        d = await asyncio.to_thread(labv().diario, dia)
        if d is None:
            raise web.HTTPNotFound(text="No hay diario del laboratorio vivo de ese día")
        return j(d)

    async def api_lab_cola(_request: web.Request) -> web.Response:
        return j(await asyncio.to_thread(labv().cola))

    async def api_lab_generador(request: web.Request) -> web.Response:
        return j(await asyncio.to_thread(labv().generador, q_int(request, "limite", 10, 1, 50)))

    async def api_real(_request: web.Request) -> web.Response:
        """Panel «Cuenta real» (docs/06, contrato de cableado): RealBroker.vista_real() o, sin el conector, «apagado».
        Nunca lleva claves."""
        if sv.real is None:
            return j(real_apagado())
        return j(sv.real.vista_real())

    app = web.Application()
    app.on_shutdown.append(close_ws)
    app.router.add_get("/", index)
    app.router.add_get("/events", events)
    app.router.add_get("/health", health_route)
    app.router.add_get("/api/estado", api_state)
    app.router.add_get("/api/noticias", api_news)
    app.router.add_get("/api/fuentes", api_sources)
    app.router.add_get("/api/tendencia", api_trend)
    app.router.add_get("/api/predicciones", api_predictions)
    app.router.add_get("/api/predicciones/evaluadas", api_predictions_done)
    app.router.add_get("/api/backtest", api_backtest)
    app.router.add_get("/api/resultados", api_results)
    app.router.add_get("/api/cartera", api_portfolio)
    app.router.add_get("/api/fase2", api_phase2)
    app.router.add_get("/api/aperturas", api_openings)
    app.router.add_get("/api/backtest/aperturas", api_bt_openings)
    app.router.add_get("/api/backtest/impulso", api_bt_impulso)
    app.router.add_get("/api/noticias_fiables", api_fiables)
    app.router.add_get("/api/decisiones", api_decisiones)
    app.router.add_get("/api/velas", api_velas)
    app.router.add_get("/api/real", api_real)
    app.router.add_get("/api/ranking", api_ranking)
    app.router.add_get("/api/lab_vivo", api_lab_vivo)
    app.router.add_get("/api/lab_vivo/hipotesis", api_lab_hipotesis)
    app.router.add_get("/api/lab_vivo/diario", api_lab_diario)
    app.router.add_get("/api/lab_vivo/cola", api_lab_cola)
    app.router.add_get("/api/lab_vivo/generador", api_lab_generador)
    app.router.add_get("/api/vigilancia", api_vigilancia)
    app.router.add_get("/api/cripto_aperturas", api_cripto_aperturas)
    app.router.add_get("/api/fuentes_vivas", api_fv)
    app.router.add_get("/api/fuentes_vivas/lista", api_fv_lista)
    app.router.add_get("/api/fuentes_vivas/candidatos", api_fv_candidatos)
    app.router.add_get("/api/fuentes_vivas/historial", api_fv_historial)
    app.router.add_get("/api/aprendizaje", api_apr)
    app.router.add_get("/api/aprendizaje/medios", api_apr_medios)
    app.router.add_get("/api/aprendizaje/medio", api_apr_medio)
    app.router.add_get("/api/aprendizaje/noticias", api_apr_noticias)
    app.router.add_get("/api/aprendizaje/estadisticas", api_apr_estadisticas)
    app.router.add_get("/api/aprendizaje/postmortems", api_apr_postmortems)
    app.router.add_get("/api/aprendizaje/cambios", api_apr_cambios)
    app.router.add_get("/api/evolucion", api_evo)
    app.router.add_get("/api/evolucion/historial", api_evo_historial)
    app.router.add_get("/api/evolucion/informes", api_evo_informes)
    app.router.add_get("/api/evolucion/propuestas", api_evo_propuestas)
    return app
