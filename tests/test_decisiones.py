"""Por qué entra o no en cada apertura (bot/decisiones.py, tabla «decisiones_apertura», contrato 2.17): una apertura
completa simulada con velas sintéticas, el motor con el paper de verdad (riesgo, aprobación y ejecución), cada motivo de
no entrar, el rechazo por exposición, el cierre con su R, el reinicio a mitad (sin repetir la entrada), las aperturas que
pasaron con el bot parado, la entrada por noticia, la API, la web contra la ruta real, la migración y que escribir no pare
el bucle. Sin red; las bases van a los temporales de conftest (nunca a M:\\BotFundamental-datos)."""
import contextlib
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot import apertura as ap, config, decisiones as D, entrada_noticias as en, fiables, paper
from bot.bus import Bus
from bot.paper import PaperBroker
from bot.server import Services, make_app
from bot.store import Store

MIN, H, DAY = ap.MIN, ap.H, ap.DAY
T = int(datetime(2026, 10, 29, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)   # jueves: apertura de Tokio y Seúl
VID = "tokio-seul-2026-10-29"
SEIS = {k: v for k, v in ap.SEIS.items() if k != "estrategia"}   # «6 diarias» entero (todas las ventanas y monedas)


def bar(t, o, h, l, c, v=30.0):
    return [t, o, h, l, c, v, v * c, v / 2]


def sesion(open_ms, d, move, n=200, px=100.0, desde=0):
    """Velas de 1 min desde la apertura: se mueve move % a favor de d en los 15 primeros min y luego queda plana. Con
    desde > 0 faltan las primeras velas (sin la de la apertura no se puede medir)."""
    out = []
    for k in range(n):
        c = px * (1 + d * move / 100 * min(k + 1, 15) / 15)
        o = out[-1][4] if out else px
        out.append(bar(open_ms + k * MIN, o, max(o, c) + 0.001, min(o, c) - 0.001, c))
    return out[desde:]


class Datos:
    """Binance de mentira: velas de 1 min de cada moneda (solo las ya cerradas) de la apertura de Tokio y Seúl (a otra
    ventana del mismo día, como la de China a +90 min, no le da velas), cierres diarios de BTC al alza y sin velas de 1 h
    (sin dato de volatilidad, que «6 diarias» no usa)."""

    def __init__(self, now, moves, desde=0):
        self.now = now
        self.m1 = {c: sesion(T, d, mv, desde=desde) for c, (d, mv) in moves.items()}

    async def fetch_interval(self, coin, interval, start, limit):
        if interval == "1d":
            d0 = T // DAY - 100
            return [bar((d0 + k) * DAY, 100 + k, 101 + k, 99 + k, 100 + k) for k in range(100)]
        if interval == "1h" or not T <= start < T + 60 * MIN:
            return []
        return [b for b in self.m1.get(coin, []) if start <= b[0] < start + limit * MIN and b[0] + MIN <= self.now[0]]


def motor(now, moves, bus, submit, get_pf, dec, params=None, signal=None, desde=0):
    eng = ap.OpeningEngine(bus, None, signal or (lambda c, w: {"dir": 0, "strength": 0.0, "why": "sin noticias"}),
                           submit, get_pf, clock=lambda: now[0], params={"estrategia": "impulso", **SEIS, **(params or {})},
                           decisiones=dec)
    eng.fetch_interval = Datos(now, moves, desde).fetch_interval
    return eng


def enganchar(bus, fn):
    """Como __main__.observe_bus: el gancho ve cada mensaje después de publicarlo."""
    publish = bus.publish

    def publish_and_observe(msg):
        out = publish(msg)
        fn(msg)
        return out

    bus.publish = publish_and_observe


def entorno(path, now, prices, funding=0.0):
    """Base, bus, decisiones (enganchadas al bus) y paper de verdad, con precios de OKX EU sin diferencial."""
    path.mkdir(parents=True, exist_ok=True)
    store = Store(path / "bot.db")
    bus = Bus(store)
    dec = D.Decisiones(bus, store, clock=lambda: now[0])
    enganchar(bus, dec.observe)
    bus.publish({"type": "market", "prices": dict(prices), "spread": {c: 0.0 for c in prices},
                 "flow": {c: {"funding": funding} for c in prices}})
    broker = PaperBroker(bus, store, lambda: bus.state.get("market"), clock=lambda: now[0], slip_bps=lambda: 0.0)
    return store, bus, dec, broker


def filas(dec, ventana_id=None, coin=None):
    """Filas guardadas en la base (de la más antigua a la más nueva)."""
    dec.flush()
    with dec.store.reader() as db:
        got = db.execute(f"SELECT {', '.join(D.COLS)} FROM decisiones_apertura ORDER BY id").fetchall()
    rows = [D._fila_db(r) for r in got]
    return [r for r in rows if (ventana_id is None or r["ventana_id"] == ventana_id) and (coin is None or r["coin"] == coin)]


def traza(rows):
    return [(r["fase"], r["decision"], r["motivo_codigo"]) for r in rows]


def wrun(eng, coin, key=VID):
    return eng.windows[key]["runs"][coin]


def wcoin(eng, coin, key=VID):
    return next(w for w in eng.view()["windows"] if w["id"] == key)["coins"][coin]


@pytest.fixture
def reglas(monkeypatch):
    for k, v in {"USER_RULES": True, "APPROVAL_MODE": "auto", "PAPER_EQUITY": 10000.0, "RISK_PER_TRADE_PCT": 0.5,
                 "FEE_PCT": 0.10, "MAX_POSITIONS": 4, "MAX_DAILY_LOSS_PCT": 3.0}.items():
        monkeypatch.setattr(config, k, v)


# ---------------------------------------------------------------- una apertura completa con el paper de verdad

MOVES = {"BTC": (1, 0.3), "ETH": (-1, 0.4), "SOL": (1, 0.5), "XRP": (1, 0.2)}
PX = {c: 100.0 * (1 + d * m / 100) for c, (d, m) in MOVES.items()}   # el precio de OKX EU al acabar la medida


async def apertura_simulada(path):
    """Tokio y Seúl con «6 diarias»: mide 15 min, entran BTC (largo al contado), ETH (corto en perpetuo) y SOL; XRP la
    veta el riesgo del paper (la exposición pasaría del 100 %). Reinicio a +16 min (la vela de la medida aún vale para
    entrar), BTC llega al objetivo y se cierra. Devuelve lo visto en cada paso."""
    out = {}
    now = [T - 10 * MIN]
    store, bus, dec, broker = entorno(path, now, PX)
    q = bus.subscribe()
    sent = []

    def submit(*a):
        sent.append(a[1])
        return broker.submit(*a)

    eng = motor(now, MOVES, bus, submit, broker.portfolio, dec)
    await eng.step()                                  # armada 15 min antes: aún no hay nada que decidir
    out["antes"] = filas(dec)
    now[0] = T + 2 * MIN
    await eng.step()                                  # abierta: midiendo el impulso
    out["midiendo"] = filas(dec)
    now[0] = T + 15 * MIN + 3000
    await eng.step()                                  # medida y entradas
    out["medida"] = filas(dec)
    out["positions"] = {s: dict(p) for s, p in broker.positions.items()}
    out["sent"] = list(sent)
    out["ws"] = []
    while not q.empty():
        m = json.loads(q.get_nowait())
        if m.get("type") == "decision_apertura":
            out["ws"].append(m)
    out["snapshot"] = bus.snapshot()
    out["why_xrp"] = wrun(eng, "XRP").last_why
    # Reinicio del bot a +16 min (base, bus, paper, decisiones y motor nuevos)
    dec.close()
    store.close()
    now[0] = T + 16 * MIN
    store, bus, dec, broker = entorno(path, now, PX)
    sent2 = []

    def submit2(*a):
        sent2.append(a[1])
        return broker.submit(*a)

    eng = motor(now, MOVES, bus, submit2, broker.portfolio, dec)
    await eng.step()
    out["reinicio"] = filas(dec)
    out["sent2"] = list(sent2)
    out["runs2"] = {c: (wrun(eng, c).state, wrun(eng, c).last_why, wrun(eng, c).entries) for c in MOVES}
    out["coin_xrp2"] = wcoin(eng, "XRP")
    out["positions2"] = len(broker.positions)
    out["riskko2"] = [e for e in store.events_since(now[0], ("riskko",))]
    # BTC llega al objetivo (3R): el paper la cierra
    btc = next(p for p in broker.positions.values() if p["coin"] == "BTC")
    now[0] = T + 3 * H
    bus.publish({"type": "market", "prices": {**PX, "BTC": btc["tp2"] * 1.0001}, "spread": {c: 0.0 for c in PX},
                 "flow": {c: {"funding": 0.0} for c in PX}})
    broker.step()
    await eng.step()
    out["cierre"] = filas(dec)
    out["btc_trade"] = next(t for t in broker.closed if t["coin"] == "BTC")
    out["btc_run"] = wrun(eng, "BTC").state
    return out, store, bus, dec, broker, eng


async def test_apertura_completa_entra_rechazo_por_exposicion_cierre_y_reinicio(tmp_path, reglas):
    out, store, _bus, dec, broker, _eng = await apertura_simulada(tmp_path)
    assert out["antes"] == []
    mid = out["midiendo"]
    assert traza(mid) == [("medida", "esperando", "otro")] * 4 and [r["coin"] for r in mid] == ["BTC", "ETH", "SOL", "XRP"]
    assert all(r["motivo"].startswith("Midiendo el impulso de los 15 primeros min") for r in mid)
    assert all(r["ventana_id"] == VID and r["ventana"] == "tokio-seul" and r["dia"] == "2026-10-29"
               and r["apertura_ms"] == T and r["preset"] == "seis" and r["estrategia"] == "6 diarias" for r in mid)
    rows = out["medida"]
    by = {c: [r for r in rows if r["coin"] == c] for c in MOVES}
    for c in ("BTC", "ETH", "SOL"):   # entran: medida → envío al paper → abierta (con su trade_id)
        assert traza(by[c]) == [("medida", "esperando", "otro"), ("medida", "entra", "entra"), ("entrada", "entra", "entra"),
                                ("paper", "abierta", "entra")], c
        med, env, abi = by[c][1:]
        story = env["story"]
        pos = out["positions"][story]
        assert story == f"a-tokio-seul-2026-10-29-{c.lower()}-1" and abi["story"] == story
        assert abi["trade_id"] == pos["id"] and abi["trade_id"].startswith("t") and abi["datos"]["entrada"] == pytest.approx(pos["entry"])
        assert med["impulso_pct"] == pytest.approx(MOVES[c][0] * MOVES[c][1], abs=1e-6)
        assert med["lado"] == ("largo" if MOVES[c][0] > 0 else "corto")
        assert med["datos"]["apertura"] == 100.0 and med["datos"]["precio"] == pytest.approx(PX[c])
        assert med["datos"]["rango"]["maximo"] >= med["datos"]["rango"]["minimo"] and med["datos"]["medida_min"] == 15
        assert med["datos"]["filtro_btc"] == "ninguno" and med["datos"]["noticias"]["dir"] == 0
        assert env["datos"]["plan"]["stop_pct"] == 1.5 and env["datos"]["plan"]["objetivo_R"] == 3.0
        assert env["datos"]["plan"]["neto_pct"] >= 3.0 and env["datos"]["limites"]["max_entradas"] == 6
        assert env["motivo"].startswith("Se envía al paper:") and "como mucho 24 h" in env["motivo"]
    assert by["ETH"][3]["datos"]["instrumento"] == "perpetuo" and by["BTC"][3]["datos"]["instrumento"] == "contado"
    # XRP: el riesgo del paper la veta por exposición (3 posiciones abiertas + esta > 100 % del capital)
    assert traza(by["XRP"]) == [("medida", "esperando", "otro"), ("medida", "entra", "entra"), ("entrada", "entra", "entra"),
                                ("riesgo", "rechazada", "exposicion")]
    rech = by["XRP"][3]
    assert rech["story"] == "a-tokio-seul-2026-10-29-xrp-1" and "Exposición total" in rech["motivo"]
    assert "> 100 % del capital" in rech["motivo"] and rech["datos"]["controles_fallidos"]
    assert len(out["sent"]) == 4 and len(out["positions"]) == 3 and "control de riesgo" in out["why_xrp"]
    # Cada fila salió por el WebSocket (con su id y datos como objeto) y no va en el estado inicial
    assert [m["row"]["id"] for m in out["ws"]] == [r["id"] for r in rows]
    assert all(isinstance(m["row"]["datos"], (dict, type(None))) and m["ts"] == m["row"]["ts"] for m in out["ws"])
    assert not any(m.get("type") == "decision_apertura" for m in out["snapshot"])
    # Reinicio a +16 min: nada se repite (ni la entrada vetada de XRP, cuya vela aún valdría para entrar)
    assert out["reinicio"] == rows and out["sent2"] == [] and out["riskko2"] == [] and out["positions2"] == 3
    for c in ("BTC", "ETH", "SOL"):
        assert out["runs2"][c][0] == "en_posicion" and out["runs2"][c][2] == 1
    assert out["runs2"]["XRP"][0] == "terminada" and out["runs2"]["XRP"][1] == rech["motivo"]
    assert out["coin_xrp2"]["state"] == "terminada" and out["coin_xrp2"]["why"] == rech["motivo"]
    assert out["coin_xrp2"]["signal"] == 1   # el lado de la fila
    # Cierre con su R, ligado por trade_id
    cierre = [r for r in out["cierre"] if r["fase"] == "cierre"]
    [c] = cierre
    tr = out["btc_trade"]
    assert c["coin"] == "BTC" and c["decision"] == "cerrada" and c["motivo_codigo"] == "objetivo"
    assert c["trade_id"] == tr["id"] == by["BTC"][3]["trade_id"] and c["story"] == tr["story"]
    assert c["datos"]["R"] == pytest.approx(tr["R"]) and c["datos"]["R"] == pytest.approx(2.864, abs=0.01)
    assert c["impulso_pct"] == pytest.approx(0.3) and c["lado"] == "largo" and "Objetivo" in c["motivo"]
    assert out["btc_run"] == "terminada" and len(out["cierre"]) == len(rows) + 1   # el motor no la vuelve a apuntar
    dec.close()
    store.close()


async def test_api_de_decisiones_y_la_web_contra_la_ruta_real(tmp_path, reglas):
    out, store, bus, dec, _broker, _eng = await apertura_simulada(tmp_path)
    n = len(out["cierre"])
    async with TestClient(TestServer(make_app(bus, lambda: {}, Services(decisiones=dec)))) as client:
        r = await client.get("/api/decisiones?desde=0")
        assert r.status == 200
        body = await r.json()
        assert body["n"] == n == len(body["rows"]) and body["desde"] == 0
        assert body["rows"][0]["fase"] == "cierre" and body["rows"][0]["datos"]["R"] == pytest.approx(2.864, abs=0.01)
        assert [x["ts"] for x in body["rows"]] == sorted((x["ts"] for x in body["rows"]), reverse=True)
        assert set(body["rows"][0]) == set(D.COLS)
        x = await (await client.get(f"/api/decisiones?desde=0&ventana_id={VID}&coin=XRP")).json()
        assert x["n"] == 4 and {y["coin"] for y in x["rows"]} == {"XRP"} and x["rows"][0]["motivo_codigo"] == "exposicion"
        assert (await (await client.get("/api/decisiones?desde=0&limite=3")).json())["n"] == 3
        por_defecto = await (await client.get("/api/decisiones")).json()   # 3 días desde el reloj del bot
        assert por_defecto["desde"] == T + 3 * H - 3 * DAY and por_defecto["n"] == n
        assert (await client.get("/api/decisiones?desde=hoy")).status == 400
        assert (await client.get("/api/decisiones?coin=" + "X" * 40)).status == 400
        real = await (await client.get("/api/decisiones?desde=0&limite=2000")).text()
    async with TestClient(TestServer(make_app(bus, lambda: {}, Services()))) as client:
        assert (await client.get("/api/decisiones")).status == 404   # sin el módulo no se guardan: la web lo dice
    dec.close()
    store.close()
    import test_radar_web as W
    if not W.NODE:
        pytest.skip("hace falta Node para probar la web")
    r = W.run(f"""const REAL = {real};
      H.fetchReply = url => url.startsWith("/api/decisiones") ? {{ok: true, json: async () => REAL}} : {{ok: false}};
      H.open(); reset(); await loadApi();
      S.vnow = {T + 3 * H};
      S.tab = "market"; renderAll(true); const box = H.el("#decBox").innerHTML;
      S.tab = "mando"; renderAll(true);
      return {{box, md: H.el("#mdDec").innerHTML, api: S.decApi, n: S.dec.length}};""")
    assert r["api"] == "ok" and r["n"] == n
    md, box = r["md"], r["box"]
    assert "Tokio: entra en ETH y SOL; BTC cerrada (objetivo +2,86 R); XRP rechazada (exposición)" in md
    assert "Tokio y Seúl" in box and "6 diarias" in box and "Guardado en el bot" in box
    for chip in ("Cerrada: objetivo · +2,86 R", "Abierta (paper)", "Rechazada por el riesgo"):
        assert chip in box, chip
    assert "Medida: esperando" in box and "Entrada: entra" in box and "Paper: abierta" in box and "Cierre: cerrada" in box
    assert "Exposición total" in box and "+0,30 %" in box and "-0,40 %" in box
    W._sin_null(box, md)


# ---------------------------------------------------------------- cada motivo de no entrar

FIABLE_CONTRA = {"dir": 0, "strength": 0.0, "why": "sin señal clara",
                 "fiables": [{"story": "n9", "dir": -1, "ts": T - 30 * MIN, "grupo": "medio:bueno.com",
                              "nombre": "bueno.com", "acierto": 0.7, "titular": "Hackeo en un exchange"}]}
CASOS = {
    # código: (fase, monedas y movimientos, parámetros, señal, cartera, primera hora, funding, desde)
    "impulso_nulo": ("medida", {"BTC": (0, 0.0)}, {}, None, {}, T + 2 * MIN, 0.0, 0),
    "impulso_bajo": ("medida", {"BTC": (1, 0.1)}, {"min_move_pct": 0.2}, None, {}, T + 2 * MIN, 0.0, 0),
    "veto_noticia": ("medida", {"BTC": (1, 0.3)}, {}, FIABLE_CONTRA, {}, T + 2 * MIN, 0.0, 0),
    "otro": ("medida", {"BTC": (-1, 0.3)}, {"btc_filter": "a_favor"}, None, {}, T + 2 * MIN, 0.0, 0),   # BTC al alza
    "sin_velas": ("medida", {"BTC": (1, 0.3)}, {}, None, {}, T + 2 * MIN, 0.0, 1),
    "tarde": ("entrada", {"BTC": (1, 0.3)}, {}, None, {}, T + 30 * MIN, 0.0, 0),   # el bot arranca a +30 min
    "moneda_ocupada": ("entrada", {"BTC": (1, 0.3)}, {}, None,
                       {"positions": [{"story": "a-europa-2026-10-28-btc-1", "coin": "BTC", "side": "long"}]},
                       T + 2 * MIN, 0.0, 0),
    "tope_dia": ("entrada", {"BTC": (1, 0.3)}, {"max_day_entries": 1}, None,
                 {"closed": [{"story": "nf-n1-eth", "coin": "ETH", "opened": T - 10 * MIN, "riskUsd": 50.0}]},
                 T + 2 * MIN, 0.0, 0),
    "riesgo_dia": ("entrada", {"BTC": (1, 0.3)}, {"max_day_risk_pct": 0.4}, None, {}, T + 2 * MIN, 0.0, 0),
    "reglas": ("entrada", {"BTC": (-1, 0.3)}, {}, None, {}, T + 2 * MIN, -2.0, 0),   # el corto pagaría 3,25 % de funding
    "perdida_dia": ("entrada", {"BTC": (1, 0.3)}, {}, None, {"limits": {"halted": True}}, T + 2 * MIN, 0.0, 0),
}


@pytest.mark.parametrize("codigo", list(CASOS))
async def test_cada_motivo_de_no_entrar_queda_apuntado(tmp_path, reglas, codigo):
    fase, moves, params, sig, pf_extra, first, funding, desde = CASOS[codigo]
    now = [first]
    store, bus, dec, _broker = entorno(tmp_path, now, {"BTC": 100.3}, funding)
    pf = {"positions": [], "pending": [], "scheduled": [], "closed": [], "equity": 10000.0, **pf_extra}
    sent = []
    eng = motor(now, moves, bus, lambda *a: sent.append(a) or True, lambda: pf, dec,
                {"windows": ["tokio-seul"], "coins": list(moves), **params}, (lambda c, w: sig) if sig else None, desde)
    await eng.step()
    now[0] = max(now[0], T + 15 * MIN + 3000)
    await eng.step()
    rows = filas(dec, VID, "BTC")
    last = rows[-1]
    assert (last["fase"], last["decision"], last["motivo_codigo"]) == (fase, "no_entra", codigo), traza(rows)
    run = wrun(eng, "BTC")
    assert not sent and run.done and last["motivo"] == run.last_why == wcoin(eng, "BTC")["why"]
    if first < T + 15 * MIN:
        assert traza(rows)[0] == ("medida", "esperando", "otro")
    else:   # arrancó tarde: no se apunta «esperando» de una medida que ya pasó
        assert ("medida", "esperando", "otro") not in traza(rows)
    if fase == "entrada" and codigo != "tarde":
        assert traza(rows)[-2] == ("medida", "entra", "entra")   # la medida dijo que sí; la entrada, que no
    if codigo == "impulso_nulo":
        assert last["impulso_pct"] == 0.0 and last["lado"] is None and "sin movimiento" in last["motivo"]
    if codigo == "veto_noticia":
        assert last["datos"]["noticias"]["fiables"][0]["nombre"] == "bueno.com" and "grupo fiable" in last["motivo"]
    if codigo in ("tope_dia", "riesgo_dia"):
        assert last["datos"]["limites"]["entradas"] == (1 if codigo == "tope_dia" else 0)
    if codigo == "reglas":
        assert "Regla del usuario" in last["motivo"] and last["datos"]["plan"]["funding_pct"] > 3
    await eng.step()   # nada nuevo en la ronda siguiente
    assert filas(dec, VID, "BTC") == rows
    dec.close()
    store.close()


async def test_paper_detenido_y_campeon_pausado_son_otro(tmp_path, reglas):
    now = [T + 15 * MIN + 3000]
    store, bus, dec, _broker = entorno(tmp_path, now, {"BTC": 100.3})
    pf = {"positions": [], "pending": [], "closed": [], "halted": True, "limits": {"halted": False}}
    eng = motor(now, {"BTC": (1, 0.3)}, bus, lambda *a: True, lambda: pf, dec, {"windows": ["tokio-seul"], "coins": ["BTC"]})
    await eng.step()
    last = filas(dec, VID, "BTC")[-1]
    assert traza([last]) == [("entrada", "no_entra", "otro")] and "el paper está detenido" in last["motivo"]
    dec.close()
    store.close()


# ---------------------------------------------------------------- la aprobación y la ejecución del paper (modo veto)

async def test_paper_rechazada_por_ti_y_aprobada_tarde_ligadas_por_la_story(tmp_path, reglas, monkeypatch):
    monkeypatch.setattr(config, "APPROVAL_MODE", "veto")
    now = [T + 15 * MIN + 3000]
    moves = {"BTC": (1, 0.3), "ETH": (1, 0.3)}
    store, bus, dec, broker = entorno(tmp_path, now, {"BTC": 100.3, "ETH": 100.3})
    eng = motor(now, moves, bus, broker.submit, broker.portfolio, dec, {"windows": ["tokio-seul"], "coins": list(moves)})
    await eng.step()
    assert set(broker.pending) == {"a-tokio-seul-2026-10-29-btc-1", "a-tokio-seul-2026-10-29-eth-1"}
    broker.decide("a-tokio-seul-2026-10-29-btc-1", False)        # la rechazas desde la web
    now[0] += 90_000                                             # el paper no da su ronda hasta 90 s después
    broker.step()                                                # sin veto: se aprueba, pero ya es tarde (maxAgeMs)
    now[0] += 30_000
    await eng.step()                                             # el motor ve que el paper no las abrió
    btc, eth = filas(dec, VID, "BTC"), filas(dec, VID, "ETH")
    assert traza(btc)[-2:] == [("entrada", "entra", "entra"), ("paper", "rechazada", "aprobacion")]
    assert btc[-1]["motivo"] == "No se aprobó: Rechazada por ti" and btc[-1]["story"] == "a-tokio-seul-2026-10-29-btc-1"
    assert traza(eth)[-2:] == [("entrada", "entra", "entra"), ("paper", "rechazada", "aprobacion")]
    assert "después de enviarla" in eth[-1]["motivo"]
    assert wrun(eng, "BTC").done and "no llegó a ejecutarla" in wrun(eng, "BTC").last_why   # sin otra fila
    dec.close()
    store.close()


# ---------------------------------------------------------------- reinicio con la medida hecha y la entrada por llegar

async def test_reinicio_antes_de_la_hora_de_entrada_usa_las_noticias_de_la_fila(tmp_path, reglas):
    """E2 entra a +30 min. Reinicio a +20 min con las noticias ya en contra: manda lo decidido al medir (la fila), no se
    repite la medida y entra a su hora una sola vez."""
    e2 = {k: v for k, v in ap.IMPULSO.items() if k != "estrategia"}
    params = {**e2, "windows": ["tokio-seul"], "coins": ["BTC"], "rvol_min": None, "btc_filter": "ninguno"}
    sig = {"s": {"dir": 0, "strength": 0.0, "why": "sin noticias", "drivers": []}}
    now = [T + 15 * MIN + 3000]
    store, bus, dec, broker = entorno(tmp_path, now, {"BTC": 100.3})
    eng = ap.OpeningEngine(bus, None, lambda c, w: sig["s"], broker.submit, broker.portfolio, clock=lambda: now[0],
                           params={"estrategia": "impulso", **params}, decisiones=dec)
    eng.fetch_interval = Datos(now, {"BTC": (1, 0.3)}).fetch_interval
    await eng.step()
    medida = [("medida", "esperando", "otro"), ("medida", "entra", "entra")]
    assert traza(filas(dec)) == medida and wrun(eng, "BTC").state == "esperando_entrada"
    dec.close()
    store.close()
    now[0] = T + 20 * MIN
    sig["s"] = {"dir": -1, "strength": 0.9, "why": "noticias en contra", "drivers": [{"factor": "x", "peso": -0.4}]}
    store, bus, dec, broker = entorno(tmp_path, now, {"BTC": 100.3})
    eng = ap.OpeningEngine(bus, None, lambda c, w: sig["s"], broker.submit, broker.portfolio, clock=lambda: now[0],
                           params={"estrategia": "impulso", **params}, decisiones=dec)
    eng.fetch_interval = Datos(now, {"BTC": (1, 0.3)}).fetch_interval
    await eng.step()
    run = wrun(eng, "BTC")
    assert run.state == "esperando_entrada" and run.news["dir"] == 0 and traza(filas(dec)) == medida
    now[0] = T + 30 * MIN + 3000
    await eng.step()
    await eng.step()
    assert traza(filas(dec)) == medida + [("entrada", "entra", "entra"), ("paper", "abierta", "entra")]
    assert len(broker.positions) == 1
    dec.close()
    store.close()


@pytest.mark.parametrize("reinicio", [5 * MIN, 15 * MIN + 30_000, 16 * MIN])
async def test_reinicio_cerca_de_la_medida_no_repite_esperando_ni_la_medida(tmp_path, reglas, reinicio):
    """E2 (entrada a +30 min). Reinicio midiendo (+5 min) o justo tras la medida (+15,5 y +16 min, aún dentro del
    margen de «esperando»): ni «esperando» ni la medida se repiten, y entra una sola vez."""
    e2 = {k: v for k, v in ap.IMPULSO.items() if k != "estrategia"}
    params = {"estrategia": "impulso", **e2, "windows": ["tokio-seul"], "coins": ["BTC"], "rvol_min": None,
              "btc_filter": "ninguno"}
    now = [T + 2 * MIN]

    def arranca():
        store, bus, dec, broker = entorno(tmp_path, now, {"BTC": 100.3})
        eng = ap.OpeningEngine(bus, None, lambda c, w: {"dir": 0, "strength": 0.0, "why": "sin noticias", "drivers": []},
                               broker.submit, broker.portfolio, clock=lambda: now[0], params=params, decisiones=dec)
        eng.fetch_interval = Datos(now, {"BTC": (1, 0.3)}).fetch_interval
        return store, dec, broker, eng

    store, dec, broker, eng = arranca()
    await eng.step()
    if reinicio > 15 * MIN:
        now[0] = T + 15 * MIN + 3000
        await eng.step()
    antes = traza(filas(dec))
    assert antes == [("medida", "esperando", "otro")] + ([("medida", "entra", "entra")] if reinicio > 15 * MIN else [])
    dec.close()
    store.close()
    now[0] = T + reinicio
    store, dec, broker, eng = arranca()
    assert now[0] < T + eng.p["measure_min"] * MIN + eng._lag_ms   # aún en el margen en que se apunta «esperando»
    await eng.step()
    assert traza(filas(dec)) == antes
    now[0] = T + 30 * MIN + 3000
    await eng.step()
    await eng.step()
    assert traza(filas(dec)) == [("medida", "esperando", "otro"), ("medida", "entra", "entra"),
                                 ("entrada", "entra", "entra"), ("paper", "abierta", "entra")]
    assert len(broker.positions) == 1
    dec.close()
    store.close()


# ---------------------------------------------------------------- volumen relativo y señales raras

async def test_entrada_con_filtro_de_volumen_apunta_el_rvol_medido(tmp_path, reglas):
    """Con rvol_min, la fila de la entrada lleva el volumen relativo medido (aquí 3: las sesiones anteriores tienen un
    tercio del volumen) y el mínimo; con «6 diarias» (sin ese filtro) los dos van null."""
    now = [T + 15 * MIN + 3000]
    store, bus, dec, broker = entorno(tmp_path, now, {"BTC": 100.3})
    eng = motor(now, {"BTC": (1, 0.3)}, bus, broker.submit, broker.portfolio, dec,
                {"windows": ["tokio-seul"], "coins": ["BTC"], "rvol_min": 1.2})
    eng._schedule(now[0])
    prev = eng.windows[VID]["prev"]
    assert len(prev) >= 2
    hoy = Datos(now, {"BTC": (1, 0.3)}).fetch_interval

    async def fetch(coin, interval, start, limit):
        po = next((x for x in prev if x <= start < x + 60 * MIN), None)
        if interval == "1m" and po is not None:   # sesiones anteriores, planas y con un tercio del volumen
            return [bar(po + k * MIN, 100.0, 100.001, 99.999, 100.0, 10.0) for k in range(limit)
                    if start <= po + k * MIN]
        return await hoy(coin, interval, start, limit)

    eng.fetch_interval = fetch
    await eng.step()
    env = next(r for r in filas(dec, VID, "BTC") if r["fase"] == "entrada")
    assert env["decision"] == "entra" and env["datos"]["rvol_min"] == 1.2
    assert env["datos"]["rvol"] == pytest.approx(3.0, abs=0.01) == round(wrun(eng, "BTC").rvol, 3)
    dec.close()
    store.close()
    now[0] = T + 15 * MIN + 3000
    store, bus, dec, broker = entorno(tmp_path / "seis", now, {"BTC": 100.3})
    eng = motor(now, {"BTC": (1, 0.3)}, bus, broker.submit, broker.portfolio, dec,
                {"windows": ["tokio-seul"], "coins": ["BTC"]})
    await eng.step()
    env = next(r for r in filas(dec, VID, "BTC") if r["fase"] == "entrada")
    assert env["decision"] == "entra" and env["datos"]["rvol"] is None and env["datos"]["rvol_min"] is None
    dec.close()
    store.close()


@pytest.mark.parametrize("news", [
    {"dir": 0, "strength": 0.0, "drivers": [{"factor": "x", "peso": 1}]},
    {"dir": 0, "strength": 0.0, "drivers": {"factor": "x"}},
    {"dir": 0, "strength": 0.0, "fiables": {"a": 1}},
    {"dir": 0, "strength": 0.0, "drivers": "x", "fiables": "n1"},
], ids=["lista", "drivers_dict", "fiables_dict", "textos"])
def test_una_senal_rara_no_cambia_la_decision_de_entrar(news, monkeypatch):
    """Los datos del registro se calculan dentro de la decisión: con cualquier forma de la señal se entra igual."""
    run = ap.ImpulseRun("BTC", T, SEIS, {"btc_dir": 1, "atr_pct": None, "prev_react": None}, news, None)
    acts = [a["action"] for b in sesion(T, 1, 0.3, n=20) if (a := run.feed(b))]
    assert acts == ["enter"] and run.state == "en_posicion"
    med = next(d for d in run.decs if d["fase"] == "medida")
    assert med["decision"] == "entra"
    assert med["datos"]["noticias"]["factores"] == ([{"factor": "x", "peso": 1}] if isinstance(news.get("drivers"), list)
                                                    else [])
    assert med["datos"]["noticias"]["fiables"] == []
    # Aunque preparar los datos falle del todo, la decisión es la misma (la fila va sin datos)

    def roto(self):
        raise RuntimeError("roto")

    monkeypatch.setattr(ap.ImpulseRun, "_measure_data", roto)
    run = ap.ImpulseRun("BTC", T, SEIS, {"btc_dir": 1, "atr_pct": None, "prev_react": None}, news, None)
    assert [a["action"] for b in sesion(T, 1, 0.3, n=20) if (a := run.feed(b))] == ["enter"]
    assert next(d for d in run.decs if d["fase"] == "medida")["datos"] is None
    run = ap.ImpulseRun("BTC", T, SEIS, {"btc_dir": 1, "atr_pct": None, "prev_react": None}, news, None)
    assert [a["action"] for b in sesion(T, 0, 0.0, n=20) if (a := run.feed(b))] == ["end"]   # y el no entra, igual
    assert run.decs[-1]["motivo_codigo"] == "impulso_nulo" and run.decs[-1]["datos"] is None
    assert ap.senal_de_fila({"datos": {"noticias": {"dir": 1, "factores": {"x": 1}, "fiables": "n1"}}}) == \
        {"dir": 1, "strength": 0.0, "why": "", "drivers": []}
    assert ap.senal_de_fila({"datos": "texto"}) is None


# ---------------------------------------------------------------- aperturas que pasaron con el bot parado

async def test_bot_parado_una_perdida_por_moneda_y_al_reiniciar_su_ultima_fila(tmp_path, reglas):
    now = [T + 3 * H]   # la sesión (2 h) acabó hace 1 h y el bot no estaba
    store, bus, dec, broker = entorno(tmp_path, now, {"BTC": 100.3, "ETH": 99.6})
    params = {"windows": ["tokio-seul"], "coins": ["BTC", "ETH"]}
    eng = motor(now, {"BTC": (1, 0.3), "ETH": (-1, 0.4)}, bus, broker.submit, broker.portfolio, dec, params)
    await eng.step()
    await eng.step()
    rows = filas(dec)
    assert traza(rows) == [("perdida", "perdida", "bot_parado")] * 2 and [r["coin"] for r in rows] == ["BTC", "ETH"]
    assert all("pasó con el bot parado" in r["motivo"] and r["datos"]["arranque"] == now[0] for r in rows)
    assert all(r["impulso_pct"] is None and r["lado"] is None for r in rows)
    dec.close()
    store.close()
    store, bus, dec, broker = entorno(tmp_path, now, {"BTC": 100.3, "ETH": 99.6})   # otro reinicio: no se repiten
    eng = motor(now, {"BTC": (1, 0.3), "ETH": (-1, 0.4)}, bus, broker.submit, broker.portfolio, dec, params)
    await eng.step()
    assert filas(dec) == rows and wcoin(eng, "BTC")["state"] == "terminada"
    assert wcoin(eng, "BTC")["why"] == rows[0]["motivo"]
    dec.close()
    store.close()


async def test_apertura_seguida_por_una_version_anterior_no_es_bot_parado(tmp_path, reglas):
    now = [T + 3 * H]
    store, bus, dec, broker = entorno(tmp_path, now, {"BTC": 100.3})
    bus.publish({"type": "event", "kind": "captured", "agent": "hunter", "story": "n1", "ts": T + 10 * MIN, "title": "x",
                 "patch": {"headline": "x"}})   # el bot de antes estaba en marcha (captaba noticias)
    eng = motor(now, {"BTC": (1, 0.3)}, bus, broker.submit, broker.portfolio, dec,
                {"windows": ["tokio-seul"], "coins": ["BTC"]})
    await eng.step()
    w = next(x for x in eng.view()["windows"] if x["id"] == VID)
    assert filas(dec) == [] and "versión anterior" in w["why"] and "bot parado" not in w["why"]
    dec.close()
    store.close()


async def test_sesion_acabada_con_decisiones_guardadas_sale_de_sus_filas(tmp_path, reglas):
    """El bot se paró a mitad: ETH ya había decidido (no entra), BTC seguía midiendo y SOL aún no tenía ninguna fila.
    Vuelve con la sesión acabada: ETH sale de su fila (no «pasó con el bot parado»), BTC y SOL quedan perdidas una vez
    (SOL también sale en la apertura de la web) y otro reinicio no repite nada."""
    now = [T + 3 * H]
    moves = {"BTC": (1, 0.3), "ETH": (0, 0.0), "SOL": (1, 0.5)}
    params = {"windows": ["tokio-seul"], "coins": list(moves)}
    store, bus, dec, broker = entorno(tmp_path, now, {"BTC": 100.3, "ETH": 100.0, "SOL": 100.5})
    bus.publish({"type": "event", "kind": "captured", "agent": "hunter", "story": "n1", "ts": T + 10 * MIN, "title": "x",
                 "patch": {"headline": "x"}})   # el bot estaba en marcha al principio de la sesión
    base = dict(ventana="tokio-seul", apertura_ms=T, preset=None, estrategia="impulso")
    dec.add(VID, "BTC", "medida", "esperando", "otro", "Midiendo el impulso", ts=T + MIN, **base)
    dec.add(VID, "ETH", "medida", "no_entra", "impulso_nulo", "Impulso de +0,00 % en 15 min: sin movimiento", ts=T + 15 * MIN,
            impulso_pct=0.0, **base)
    eng = motor(now, moves, bus, broker.submit, broker.portfolio, dec, params)
    await eng.step()
    await eng.step()
    w = next(x for x in eng.view()["windows"] if x["id"] == VID)
    assert "bot parado" not in w["why"] and w["coins"]["ETH"]["why"] == "Impulso de +0,00 % en 15 min: sin movimiento"
    btc, sol = filas(dec, VID, "BTC"), filas(dec, VID, "SOL")
    assert traza(btc) == [("medida", "esperando", "otro"), ("perdida", "perdida", "bot_parado")]
    assert "midiendo el impulso" in btc[-1]["motivo"] and len(filas(dec, VID, "ETH")) == 1
    assert traza(sol) == [("perdida", "perdida", "bot_parado")] and "antes de decidir esta moneda" in sol[0]["motivo"]
    assert w["coins"]["SOL"]["why"] == sol[0]["motivo"] and sol[0]["datos"]["fin_sesion"] == T + 2 * H
    rows = filas(dec)
    dec.close()
    store.close()
    store, bus, dec, broker = entorno(tmp_path, now, {"BTC": 100.3, "ETH": 100.0, "SOL": 100.5})   # otro reinicio
    eng = motor(now, moves, bus, broker.submit, broker.portfolio, dec, params)
    await eng.step()
    assert filas(dec) == rows and wcoin(eng, "SOL")["why"] == sol[0]["motivo"]
    dec.close()
    store.close()


@pytest.mark.parametrize("en_marcha", [True, False])
async def test_apertura_con_solo_filas_del_paper_y_monedas_sin_fila(tmp_path, reglas, en_marcha):
    """Solo está el cierre de una operación de antes de la tabla (lo apuntó el gancho del paper por su story). Las otras
    monedas no tienen fila: si el bot guardó eventos durante la sesión la siguió una versión anterior (no se inventa
    «bot parado»); si no, sí es una perdida, una vez."""
    now = [T + 3 * H]
    moves = {"BTC": (1, 0.3), "ETH": (-1, 0.4)}
    store, bus, dec, broker = entorno(tmp_path, now, {"BTC": 100.3, "ETH": 99.6})
    if en_marcha:
        bus.publish({"type": "event", "kind": "captured", "agent": "hunter", "story": "n1", "ts": T + 10 * MIN,
                     "title": "x", "patch": {"headline": "x"}})
    dec.observe({"type": "event", "kind": "closed", "story": "a-tokio-seul-2026-10-29-btc-1", "ts": T + 150 * MIN,
                 "title": "closed", "text": "", "patch": {"result": {"side": "long", "R": 2.9, "why": "Objetivo (3,0 R)"},
                                                         "trade_id": "tviejo"}})
    eng = motor(now, moves, bus, broker.submit, broker.portfolio, dec, {"windows": ["tokio-seul"], "coins": list(moves)})
    await eng.step()
    await eng.step()
    assert traza(filas(dec, VID, "BTC")) == [("cierre", "cerrada", "objetivo")]
    assert wcoin(eng, "BTC")["state"] == "terminada" and "Objetivo" in wcoin(eng, "BTC")["why"]
    eth = filas(dec, VID, "ETH")
    assert wcoin(eng, "ETH")["state"] == "terminada"
    if en_marcha:
        assert eth == [] and "versión anterior" in wcoin(eng, "ETH")["why"]
    else:
        assert traza(eth) == [("perdida", "perdida", "bot_parado")] and "pasó con el bot parado" in eth[0]["motivo"]
    dec.close()
    store.close()


# ---------------------------------------------------------------- entrada por noticia fiable (ventana «noticia»)

GRUPO = {"clave": "medio:bueno.com", "tipo": "medio", "nombre": "bueno.com", "sentido": None, "n": 40, "dias": 4,
         "acierto": 0.7, "ic90_bajo": 0.58, "estado": "fiable", "outlet": "bueno.com", "n_historias": 40, "opera": True}


async def test_entrada_por_noticia_se_apunta_con_su_ventana(tmp_path, reglas):
    import numpy as np
    ts = int(datetime(2026, 10, 28, 10, 0, 20, tzinfo=timezone.utc).timestamp() * 1000)
    ta = en.ta_de(ts)
    rows = []
    for t in range(ta - 25 * H, ta + 3 * H, MIN):
        k = (t + MIN - ta) // MIN
        c = 100.0 * (1 + 0.06 / 100 * k) if k > 0 else 100.0
        rows.append([t, c, c, c, c, 1.0])
    serie = np.array(rows, float)
    now = [ta + MIN]

    def bars(coin, a, b):
        x = serie if coin == "BTC" else None
        return None if x is None else x[(x[:, 0] >= a) & (x[:, 0] < b) & (x[:, 0] + MIN <= now[0])]

    fiables.usar({"grupos": [GRUPO]})
    try:
        store, bus, dec, broker = entorno(tmp_path, now, {"BTC": 100.6})
        m = en.EntradaNoticias(bus, store, broker.submit, broker.portfolio, bars=bars, clock=lambda: now[0], enabled=True)
        enganchar(bus, m.observe)
        for sid in ("n1", "n2"):   # dos noticias fiables de BTC: la segunda topa con la posición de la primera
            bus.publish({"type": "event", "kind": "captured", "agent": "hunter", "story": sid, "ts": ts - 5000,
                         "title": "Bitcoin: noticia", "patch": {"headline": "Bitcoin", "src": "Bueno", "srcId": "feed-bueno",
                                                                "url": "https://bueno.com/a", "clase": "cripto"}})
            bus.publish({"type": "event", "kind": "triaged", "agent": "local", "story": sid, "ts": ts, "title": "Bitcoin",
                         "patch": {"triage": {"tipo": "exchange", "importancia": 7, "credibilidad": 80, "resumen": "Bitcoin",
                                              "repercusion": {"direccion": 60, "magnitud": 70, "activos": ["BTC"]},
                                              "monedas": ["BTC"]}}})
        await m.step()
        now[0] = ta + 9 * MIN + 40_000
        await m.step()
        n1, n2 = filas(dec, "noticia-n1", "BTC"), filas(dec, "noticia-n2", "BTC")
        assert traza(n1) == [("medida", "esperando", "otro"), ("entrada", "entra", "entra"), ("paper", "abierta", "entra")]
        assert all(r["ventana"] == "noticia" and r["estrategia"] == "Entrada por noticia fiable" and r["lado"] == "largo"
                   for r in n1)
        assert n1[1]["story"] == n1[2]["story"] == "nf-n1-btc" and n1[2]["trade_id"] == broker.positions["nf-n1-btc"]["id"]
        assert n1[1]["impulso_pct"] == pytest.approx(0.54) and n1[0]["story"] == "n1"
        assert traza(n2) == [("medida", "esperando", "otro"), ("entrada", "no_entra", "moneda_ocupada")]
        assert "una sola posición por moneda" in n2[-1]["motivo"]
        dec.close()
        store.close()
    finally:
        fiables.usar(None)


def test_noticia_vetada_tras_el_riesgo_no_se_apunta_dos_veces(tmp_path):
    now = [T]
    store = Store(tmp_path / "bot.db")
    bus = Bus(store)
    dec = D.Decisiones(bus, store, clock=lambda: now[0])
    ev = lambda kind, story, patch, t=T: {"type": "event", "kind": kind, "story": story, "ts": t, "title": kind,
                                          "text": "", "patch": patch}
    dec.observe(ev("strategy", "nf-n7-eth", {"src": D.SRC_NOTICIA, "noticia": "n7", "tags": {"mov_pct": -0.6},
                                             "strat": {"coin": "ETH", "side": "short", "stopPct": 1.5, "tp2R": 3.0,
                                                       "instrument": "perp", "risk": 0.5}}))
    dec.observe(ev("riskko", "nf-n7-eth", {"risk": {"checks": [{"ok": False, "t": "Ya hay 4 posiciones"}], "ok": False,
                                                    "why": "Posiciones abiertas o en curso 4 de 4: no caben más"}}))
    dec.observe(ev("entrada_noticia", "n7", {"entrada_noticia": {"estado": "vetada", "moneda": "ETH", "dir": -1,
                                                                 "motivo": "Confirmada, pero vetada por el control de "
                                                                           "riesgo del paper: no se entra"}}))
    dec.observe(ev("entrada_noticia", "n8", {"entrada_noticia": {"estado": "caducada", "moneda": None, "dir": 1,
                                                                 "motivo": "sin monedas"}}))   # sin moneda: sin fila
    dec.observe(ev("closed", "a-europa-2026-10-28-sol-1",
                   {"result": {"side": "short", "R": -1.07, "why": "Stop de pérdida", "pnl": -0.6},
                    "trade_id": "tabc"}))   # una operación de antes de la tabla: su story dice la apertura
    rows = filas(dec)
    assert traza(rows) == [("entrada", "entra", "entra"), ("riesgo", "rechazada", "max_posiciones"),
                           ("cierre", "cerrada", "stop")]
    assert rows[0]["ventana_id"] == "noticia-n7" and rows[0]["lado"] == "corto" and rows[0]["impulso_pct"] == -0.6
    old = rows[2]
    assert old["ventana_id"] == "europa-2026-10-28" and old["coin"] == "SOL" and old["trade_id"] == "tabc"
    assert old["apertura_ms"] == int(datetime(2026, 10, 28, 8, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert old["datos"]["R"] == -1.07 and old["lado"] == "corto"
    dec.observe(ev("closed", "n-fase2-x", {"result": {"R": 1.0, "why": "Objetivo"}}))   # la fase 2 no va aquí
    dec.observe({"type": "market", "prices": {}})
    assert len(filas(dec)) == 3
    dec.close()
    store.close()


# ---------------------------------------------------------------- la tabla: catálogo, sin repetir, migración y fallos

def test_catalogo_cerrado_sin_repetir_y_ts_en_orden(tmp_path):
    store = Store(tmp_path / "bot.db")
    bus = Bus(store)
    dec = D.Decisiones(bus, store, clock=lambda: T)
    a = dec.add(VID, "BTC", "medida", "esperando", "otro", "Midiendo", ts=T)
    assert dec.add(VID, "BTC", "medida", "esperando", "otro", "Midiendo", ts=T + 5000) is None   # igual a la anterior
    b = dec.add(VID, "BTC", "medida", "no_entra", "inventado", "Otro motivo", ts=T - 9000)       # código fuera: «otro»
    assert b["motivo_codigo"] == "otro" and b["ts"] == a["ts"]   # nunca antes que la anterior de la misma apertura
    assert dec.add(VID, "BTC", "fase_rara", "entra", "entra", "x") is None
    assert dec.add(VID, "BTC", "medida", "decision_rara", "entra", "x") is None
    c = dec.add(VID, "BTC", "entrada", "no_entra", "tope_dia", "Tope", datos={"x": float("nan"), "y": [1, float("inf")]})
    assert c["datos"] == {"x": None, "y": [1, None]} and c["dia"] == "2026-10-29"
    assert set(D.MOTIVOS) == {"entra", "impulso_nulo", "impulso_bajo", "tarde", "sin_velas", "veto_noticia", "tope_dia",
                              "riesgo_dia", "perdida_dia", "exposicion", "moneda_ocupada", "max_posiciones", "reglas",
                              "aprobacion", "bot_parado", "objetivo", "stop", "tiempo", "otro"}
    assert [r["id"] for r in filas(dec)] == [a["id"], b["id"], c["id"]]
    assert "decision_apertura" not in bus.state   # solo el cable: no va en el estado inicial
    dec.close()
    store.close()


def test_migracion_aditiva_en_una_base_vieja_y_nada_se_borra(tmp_path):
    p = tmp_path / "vieja.db"
    db = sqlite3.connect(p)
    db.executescript("""
        CREATE TABLE events(id TEXT PRIMARY KEY, ts INTEGER NOT NULL, story TEXT, agent TEXT, kind TEXT, body TEXT NOT NULL);
        INSERT INTO events VALUES ('e1', 1, 's', 'hunter', 'captured', '{"id": "e1"}');
        CREATE TABLE decisiones_apertura(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, coin TEXT NOT NULL,
          decision TEXT);
        INSERT INTO decisiones_apertura(ts, coin, decision) VALUES (5, 'BTC', 'entra');""")
    db.commit()
    db.close()
    store = Store(p)   # una base sin la tabla, o con una versión anterior: se añade lo que falta
    cols = {r[1] for r in store.db.execute("PRAGMA table_info(decisiones_apertura)")}
    assert set(D.COLS) <= cols
    assert store.db.execute("SELECT ts, coin, decision FROM decisiones_apertura").fetchall() == [(5, "BTC", "entra")]
    assert store.db.execute("SELECT id FROM events").fetchall() == [("e1",)]
    idx = {r[1] for r in store.db.execute("PRAGMA index_list(decisiones_apertura)")}
    assert {"decisiones_apertura_ventana", "decisiones_apertura_ts"} <= idx
    dec = D.Decisiones(Bus(store), store, clock=lambda: T)
    assert dec.add(VID, "ETH", "medida", "esperando", "otro", "x")["id"] == 2
    dec.flush()
    with pytest.raises(sqlite3.DatabaseError):
        store.db.execute("DELETE FROM decisiones_apertura")
    with pytest.raises(sqlite3.DatabaseError):
        store.db.execute("UPDATE decisiones_apertura SET motivo = 'otro'")
    store.db.rollback()
    assert store.db.execute("SELECT COUNT(*) FROM decisiones_apertura").fetchone()[0] == 2
    dec.close()
    store.close()
    store = Store(p)   # abrirla otra vez no cambia nada
    assert store.db.execute("SELECT COUNT(*) FROM decisiones_apertura").fetchone()[0] == 2
    store.close()
    # Una versión anterior de la tabla sin AUTOINCREMENT (y ninguna otra tabla con él: no hay sqlite_sequence)
    p2 = tmp_path / "vieja_sin_autoincrement.db"
    db = sqlite3.connect(p2)
    db.executescript("""
        CREATE TABLE decisiones_apertura(id INTEGER PRIMARY KEY, ts INTEGER, coin TEXT, decision TEXT);
        INSERT INTO decisiones_apertura(ts, coin, decision) VALUES (5, 'BTC', 'entra');""")
    db.commit()
    db.close()
    store = Store(p2)
    assert store.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'").fetchone() is None
    dec = D.Decisiones(Bus(store), store, clock=lambda: T)   # arranca igual (antes: OperationalError y sin la ruta)
    assert dec.add(VID, "ETH", "medida", "esperando", "otro", "x")["id"] == 2
    dec.flush()
    assert [(r["id"], r["coin"]) for r in filas(dec)] == [(1, "BTC"), (2, "ETH")] and not dec.unsaved
    dec.close()
    store.close()


def test_escribir_no_para_el_bucle_aunque_la_base_este_ocupada(tmp_path):
    """Con otra conexión escribiendo (la base bloqueada 1 s), apuntar 100 decisiones no espera: las guarda el hilo
    cuando se libera. Mientras tanto la consulta ya las da (las aún sin guardar)."""
    store = Store(tmp_path / "bot.db")
    dec = D.Decisiones(Bus(store), store, clock=lambda: T)
    assert dec._writer is not None and dec._writer.is_alive()   # el hilo ya está en marcha antes de la primera fila
    otro = sqlite3.connect(tmp_path / "bot.db", timeout=5)
    otro.execute("BEGIN IMMEDIATE")
    otro.execute("INSERT INTO seen VALUES ('k', 1)")
    tiempos = []
    for i in range(100):
        t0 = time.perf_counter()
        dec.add(f"w{i}-2026-10-29", "BTC", "medida", "esperando", "otro", f"fila {i}", ts=T + i)
        tiempos.append(time.perf_counter() - t0)
    assert dec.consulta(desde=0)["n"] == 100
    time.sleep(1.0)
    otro.commit()
    otro.close()
    dec.flush()
    # Ninguna esperó a que se liberase la base (1 s): una escritura síncrona habría tardado al menos eso
    mediana = sorted(tiempos)[len(tiempos) // 2]
    assert max(tiempos) < 0.25 and mediana < 0.002, (max(tiempos), mediana)
    assert len(filas(dec)) == 100 and not dec.unsaved
    dec.close()
    store.close()


async def test_si_guardar_falla_queda_en_el_registro_y_el_motor_sigue(tmp_path, reglas, caplog):
    now = [T + 15 * MIN + 3000]
    store, bus, dec, broker = entorno(tmp_path, now, {"BTC": 100.3})
    store.db.executescript("CREATE TRIGGER no_cabe BEFORE INSERT ON decisiones_apertura "
                           "BEGIN SELECT RAISE(ABORT, 'disco lleno'); END;")
    ws = bus.subscribe()
    eng = motor(now, {"BTC": (1, 0.3)}, bus, broker.submit, broker.portfolio, dec,
                {"windows": ["tokio-seul"], "coins": ["BTC"]})
    with caplog.at_level(logging.ERROR, logger="decisiones"):
        await eng.step()
        dec.flush()
    assert len(broker.positions) == 1 and "no pude guardar" in caplog.text   # entra igual; el fallo queda en el registro
    assert filas(dec) == [] and dec.errores >= 1
    got = [json.loads(ws.get_nowait()) for _ in range(ws.qsize())]
    assert [m["row"]["decision"] for m in got if m.get("type") == "decision_apertura"] == ["esperando", "entra", "entra",
                                                                                         "abierta"]
    assert dec.ultima(VID, "BTC")["decision"] == "abierta"   # en memoria sigue: un reinicio de la sesión no la repite

    def roto(*a, **k):   # un registro que lanza no tumba el motor
        raise RuntimeError("roto")

    dec.add = roto
    assert eng._dec(eng.windows[VID], "BTC", now[0], "entrada", "no_entra", "otro", "x") is None
    now[0] = T + 3 * H
    await eng.step()
    assert wrun(eng, "BTC").state == "en_posicion"
    with caplog.at_level(logging.ERROR, logger="decisiones"):
        dec.close()   # al cerrar, un último intento: siguen sin caber, se dan por perdidas y queda en el registro
    assert dec.perdidas == 4 and not dec.unsaved and "no estarán en la base" in caplog.text
    store.close()


def test_lo_que_no_se_pudo_guardar_se_reintenta_y_una_id_ocupada_se_cambia(tmp_path):
    """Un fallo al guardar no pierde la fila: sigue en la consulta y se guarda en cuanto se puede. Una fila mala no tira
    el lote entero, y una id que ya está en la base (otro bot escribiendo a la vez) se cambia por una libre."""
    store = Store(tmp_path / "bot.db")
    dec = D.Decisiones(Bus(store), store, clock=lambda: T)
    dec.reintento_s = 0.02
    store.db.executescript("CREATE TRIGGER no_cabe BEFORE INSERT ON decisiones_apertura WHEN NEW.coin = 'BTC' "
                           "BEGIN SELECT RAISE(ABORT, 'disco lleno'); END;")
    a = dec.add(VID, "BTC", "medida", "esperando", "otro", "x", ts=T)
    b = dec.add(VID, "ETH", "medida", "esperando", "otro", "x", ts=T)
    dec.flush()
    assert [r["id"] for r in filas(dec)] == [b["id"]]   # la de ETH se guarda aunque la de BTC falle (mismo lote)
    assert set(dec.unsaved) == {a["id"]} and {r["id"] for r in dec.consulta(desde=0)["rows"]} == {a["id"], b["id"]}
    store.db.executescript("DROP TRIGGER no_cabe;")

    def espera(fn):
        t0 = time.monotonic()
        while not fn() and time.monotonic() - t0 < 10:
            time.sleep(0.01)
        return fn()

    assert espera(lambda: not dec.unsaved), "el reintento no la guardó"
    assert [r["id"] for r in filas(dec)] == sorted([a["id"], b["id"]]) and dec.perdidas == 0
    # Otro proceso ya usó la id que toca (dos bots a la vez): la fila va con una libre y nada se pierde
    ocupada = dec._next
    otro = sqlite3.connect(tmp_path / "bot.db")
    otro.execute("INSERT INTO decisiones_apertura(id, ts, ventana_id, coin) VALUES (?, ?, 'otro-bot', 'SOL')", (ocupada, T))
    otro.commit()
    otro.close()
    c = dec.add(VID, "SOL", "medida", "esperando", "otro", "x", ts=T)
    dec.flush()
    rows = filas(dec)
    assert c["id"] > ocupada and [(r["id"], r["ventana_id"]) for r in rows][-2:] == [(ocupada, "otro-bot"), (c["id"], VID)]
    assert not dec.unsaved and dec.ultima(VID, "SOL")["id"] == c["id"]
    assert dec.add(VID, "XRP", "medida", "esperando", "otro", "x", ts=T)["id"] == c["id"] + 1
    dec.close()
    store.close()


def test_la_consulta_no_pierde_una_fila_que_se_guarda_mientras_lee(tmp_path):
    """La fila aún sin guardar se copia antes de leer la base: si el hilo la guarda (y la saca de unsaved) justo
    después del SELECT, sigue saliendo."""
    store = Store(tmp_path / "bot.db")
    dec = D.Decisiones(Bus(store), store, clock=lambda: T)
    otro = sqlite3.connect(tmp_path / "bot.db", timeout=5)
    otro.execute("BEGIN IMMEDIATE")   # el hilo escritor espera a que se libere la base
    otro.execute("INSERT INTO seen VALUES ('k', 1)")
    dec.add(VID, "BTC", "medida", "esperando", "otro", "x", ts=T)
    orig = store.reader

    @contextlib.contextmanager
    def reader():
        with orig() as db:
            yield db
        otro.commit()   # justo después de leer: el hilo la guarda y la saca de unsaved
        dec.flush()

    store.reader = reader
    got = dec.consulta(desde=0)
    assert [r["coin"] for r in got["rows"]] == ["BTC"] and not dec.unsaved
    store.reader = orig
    assert [r["coin"] for r in dec.consulta(desde=0)["rows"]] == ["BTC"]   # ya guardada: una vez, de la base
    otro.close()
    dec.close()
    store.close()


def test_la_fase_2_con_la_story_de_la_noticia_no_se_cuela_en_la_entrada_por_noticia(tmp_path):
    """La fase 2 manda al paper con la story de la noticia (claude_agents: submit(t, sid, …)). Si la entrada por noticia
    apuntó esa noticia (esperando y caducada, con story = la noticia), lo que digan el riesgo y el paper de la operación
    de la fase 2 no va a la ventana noticia-<story>."""
    store = Store(tmp_path / "bot.db")
    dec = D.Decisiones(Bus(store), store, clock=lambda: T)
    ev = lambda kind, story, patch: {"type": "event", "kind": kind, "story": story, "ts": T + 1000, "title": kind,
                                     "text": "", "patch": patch}
    for estado, extra in (("esperando", {}), ("caducada", {"motivo_codigo": "impulso_bajo"})):
        dec.observe(ev("entrada_noticia", "n42", {"entrada_noticia": {"estado": estado, "moneda": "BTC", "dir": 1,
                                                                       "motivo": estado, **extra}}))
    antes = filas(dec)
    assert traza(antes) == [("medida", "esperando", "otro"), ("entrada", "no_entra", "impulso_bajo")]
    assert all(r["story"] == "n42" for r in antes)   # la web la usa para el titular
    dec.observe(ev("riskko", "n42", {"risk": {"checks": [], "ok": False, "why": "Exposición total 120 % > 100 %"}}))
    dec.observe(ev("blocked", "n42", {"exec": {"why": "Ya hay 4 posiciones abiertas (máximo 4)"}}))
    dec.observe(ev("filled", "n42", {"exec": {"px": 3000}, "trade_id": "tf2"}))
    dec.observe(ev("closed", "n42", {"result": {"R": 1.5, "why": "Objetivo (2,0 R)", "pnl": 10}, "trade_id": "tf2"}))
    assert filas(dec) == antes and dec.de_story("n42") is None
    dec.close()
    store.close()


def test_motivos_del_paper_con_sus_textos():
    m = paper.motivo_codigo
    assert m("Exposición total 101,9 % > 100 % del capital") == "exposicion"
    assert m("La exposición subiría al 133 % del capital (máximo 100 %)", "blocked") == "exposicion"
    assert m("Nominal en perpetuos 210 % > 200 % del capital (2:1, máximo para particulares en España)") == "exposicion"
    assert m("Pérdida del día 3,10 % ≥ 3,00 % como máximo") == "perdida_dia"
    assert m("Operativa detenida por la pérdida máxima del día hasta la medianoche de Madrid", "blocked") == "perdida_dia"
    assert m("Posiciones abiertas o en curso 4 de 4: no caben más") == "max_posiciones"
    assert m("Ya hay 4 posiciones abiertas (máximo 4)", "blocked") == "max_posiciones"
    assert m("Ya hay una operación en BTC (a-europa-2026-10-28-btc-1): una sola por moneda") == "moneda_ocupada"
    assert m("Ya hay una operación en curso para esta noticia") == "otro"
    assert m("Regla del usuario: stop de 1,00 % por debajo del mínimo de 1,50 %") == "reglas"
    assert m("Aprobada 90 s después de enviarla (máximo 60 s): la señal y el plan de salidas ya son viejos",
             "blocked") == "aprobacion"
    assert m("Rechazada por ti", "humanko") == "aprobacion" and m("Caducada sin respuesta", "humanko") == "aprobacion"
    assert m(paper.HALT_WHY) == "otro" and m(f"{paper.HALT_WHY}: no se aprueba sola", "humanko") == "otro"
    assert m("Diferencial de BTC: 6,0 pb > 5 pb") == "otro" and m(None) == "otro"
    assert D.codigo_cierre("Objetivo (3,0 R)") == "objetivo" and D.codigo_cierre("Stop de pérdida") == "stop"
    assert D.codigo_cierre("Cierre por tiempo (tope de 24 h)") == "tiempo" and D.codigo_cierre("Cierre manual") == "otro"
