import asyncio
import functools
import json
import random
import sqlite3
import threading
import time
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import WSCloseCode, WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from bot import config, server
from bot.bus import Bus
from bot.desk import NewsDesk, Recent
from bot.filters import similar
from bot.market import Market
from bot.models import Item, Source, now_ms
from bot.regions import REGIONS
from bot.server import Services, adjusted_credibility, make_app, recent_news
from bot.store import Store, in_batches

CD = Source("coindesk", "CoinDesk", "T2", 60, None)
TB = Source("theblock", "The Block", "T2", 60, None)


def setup(tmp_path):
    store = Store(tmp_path / "t.db")
    bus = Bus(store)
    q = bus.subscribe()
    return store, bus, NewsDesk(bus, store), q


def events(q):
    out = []
    while not q.empty():
        m = json.loads(q.get_nowait())
        if m["type"] == "event":
            out.append(m)
    return out


def test_relevante_repetida_y_confirmada(tmp_path):
    _, _, desk, q = setup(tmp_path)
    t = now_ms() - 4000
    assert desk.handle(Item("coindesk", "a", "SEC approves spot Solana ETF with staking", "u", t), CD, warm=False)
    assert not desk.handle(Item("coindesk", "a", "SEC approves spot Solana ETF with staking", "u", t), CD, warm=False)
    assert desk.handle(Item("theblock", "b", "SEC approves Solana spot ETF featuring staking", "v", t), TB, warm=False)
    cap, cor = events(q)
    assert cap["kind"] == "captured" and cap["coins"] == ["SOL"] and cap["patch"]["filter"]["ok"]
    assert cor["kind"] == "corroborated" and cor["story"] == cap["story"] and cor["patch"]["corrob"][0]["src"] == "The Block"


def test_ruido_se_publica_como_descartado(tmp_path):
    _, _, desk, q = setup(tmp_path)
    desk.handle(Item("coindesk", "c", "PEPE surges 300% after whale buys", "u", now_ms()), CD, warm=False)
    [ev] = events(q)
    assert ev["kind"] == "filtered" and "Fuera del universo" in ev["text"]


def test_historico_del_arranque_no_se_publica(tmp_path):
    _, _, desk, q = setup(tmp_path)
    old = now_ms() - 8 * 3600_000
    assert not desk.handle(Item("coindesk", "d", "Bitcoin hits record", "u", old), CD, warm=True)
    assert not desk.handle(Item("coindesk", "e", "Bitcoin hits record", "u", None), CD, warm=True)
    assert events(q) == []


def test_contexto_region_idioma_y_temas(tmp_path):
    _, _, desk, q = setup(tmp_path)
    got = []
    desk.on_relevant = lambda *a, **k: got.append((a, k))
    ES = Source("elpais", "El País", "T2", 60, None)
    t = now_ms() - 3 * 3600_000
    assert desk.handle(Item("elpais", "x", "Sánchez convoca elecciones generales anticipadas", "https://elpais.com/a", t,
                            region="es", lang="es"), ES, warm=False) == "captured"
    [ev] = events(q)
    p = ev["patch"]
    assert p["region"] == "es" and p["lang"] == "es" and p["fresh"] is False and "elecciones" in p["topics"]
    assert "Contexto" in p["filter"]["why"] and p["clase"] == "contexto" and ev["coins"] == []
    assert got == []   # política: se capta (y se mide), pero ya no gasta IA local
    # Una macro de hace horas se capta, pero ya no gasta IA local (n.º 1 y 42): solo contaría si la cripto la reflejara
    # en los 60 min desde la captura, y ese movimiento ya pasó
    assert desk.handle(Item("elpais", "y", "El BCE sube los tipos de interés medio punto", "https://elpais.com/b", t,
                            region="es", lang="es"), ES, warm=False) == "captured"
    [ev] = events(q)
    assert ev["patch"]["clase"] == "macro" and ev["patch"]["macro"] is True and ev["coins"] == [] and got == []
    # La misma macro, fresca, sí va; y la cripto de hace horas también (la cripto se tría siempre)
    assert desk.handle(Item("elpais", "z", "Rusia lanza misiles contra Kiev", "https://elpais.com/c",
                            now_ms(), region="es", lang="es"), ES, warm=False) == "captured"
    assert desk.handle(Item("elpais", "w", "Bitcoin cae un 5 % tras el ataque", "https://elpais.com/d", t,
                            region="es", lang="es"), ES, warm=False) == "captured"
    (a1, k1), (a2, k2) = got
    assert k1["region"] == "es" and k2["prio"] > k1["prio"]   # la cripto, aunque sea de hace horas, va antes


def test_mismo_medio_por_otro_feed_no_confirma(tmp_path):
    _, _, desk, q = setup(tmp_path)
    A, B, C = (Source(i, n, "T1", 60, None) for i, n in (("rt-mkts", "Reuters Markets"), ("rt-world", "Reuters World"), ("ap", "AP")))
    t = now_ms()
    assert desk.handle(Item("rt-mkts", "1", "US imposes 50% tariffs on Chinese electric vehicles", "https://www.reuters.com/a", t), A, False)
    assert desk.handle(Item("rt-world", "2", "US imposes 50% tariffs on Chinese electric vehicles", "https://www.reuters.com/b", t), B, False) is None
    assert desk.handle(Item("ap", "3", "US imposes 50% tariffs on Chinese electric vehicles", "https://apnews.com/c", t,
                            region="us"), C, False) == "corroborated"
    cap, cor = events(q)
    assert [c["src"] for c in cor["patch"]["corrob"]] == ["AP"] and cor["patch"]["corrob"][0]["region"] == "us"


def test_descartes_del_arranque_no_se_publican_y_titulo_largo_se_corta(tmp_path):
    _, _, desk, q = setup(tmp_path)
    assert desk.handle(Item("coindesk", "g", "Real Madrid wins the league", "u", now_ms() - 600_000), CD, warm=True) == "filtered"
    assert events(q) == []
    desk.handle(Item("coindesk", "h", "Bitcoin " + "x" * 400, "u", now_ms()), CD, warm=False)
    [ev] = events(q)
    assert len(ev["title"]) == 280 and ev["title"].endswith("…")


async def test_servidor_web_y_websocket(tmp_path):
    store, bus, desk, _ = setup(tmp_path)
    desk.handle(Item("coindesk", "f", "Ether ETF inflows hit record", "u", now_ms()), CD, warm=False)
    async with TestClient(TestServer(make_app(bus, lambda: {"ok": True}))) as client:
        r = await client.get("/")
        html = await r.text()
        assert html.startswith("<!doctype html>") and "window.RADAR_LIVE" in html
        ws = await client.ws_connect("/events")
        snap = json.loads((await ws.receive()).data)
        assert snap[0]["type"] == "reset" and any(m.get("kind") == "captured" for m in snap)
        bus.publish({"type": "market", "prices": {"BTC": 81000.0}})
        live = json.loads((await ws.receive()).data)
        assert live["type"] == "market" and live["prices"]["BTC"] == 81000.0
        await ws.close()
        for path in ("/api/estado", "/api/noticias", "/api/fuentes", "/api/tendencia", "/api/predicciones",
                     "/api/backtest", "/api/resultados", "/api/fase2"):
            assert (await client.get(path)).status == 200, path
        assert (await client.get("/api/noticias?bolsa=marte")).status == 400
    store.close()


async def test_decision_de_la_web_llega_al_broker(tmp_path):
    store, bus, _, _ = setup(tmp_path)

    class Broker:
        calls = []

        def decide(self, story, ok, by=None):
            self.calls.append((story, ok, by))
            return True

    b = Broker()
    async with TestClient(TestServer(make_app(bus, lambda: {}, Services(broker=b)))) as client:
        ws = await client.ws_connect("/events")
        await ws.receive()
        await ws.send_str(json.dumps({"type": "decision", "story": "n1", "ok": True}))
        await ws.send_str("esto no es json")
        await ws.send_str(json.dumps({"type": "decision", "story": "n2", "ok": False}))
        for _ in range(50):
            if len(b.calls) == 2:
                break
            await asyncio.sleep(0.02)
        await ws.close()
    assert b.calls == [("n1", True, "Aprobada por ti"), ("n2", False, "Rechazada por ti")]
    store.close()


def test_credibilidad_ajustada():
    assert adjusted_credibility(80, "T0", 2) == 95
    assert adjusted_credibility(80, "T3", 0) == 75
    assert adjusted_credibility(98, "T1", 5) == 100
    assert adjusted_credibility(None, "T1", 1) is None


# --- Revisión: servidor, bus, base y mesa ---

def _old_recent_news(bus, hours, min_imp, bolsa=None):
    """La versión anterior de recent_news (todo en Python): referencia para comprobar que la nueva elige lo mismo."""
    evs = bus.store.events_since(now_ms() - int(hours * 3600_000), ("captured", "triaged", "lowprio", "corroborated"))
    by = {}
    for e in evs:
        s, p = by.setdefault(e["story"], {}), e.get("patch", {})
        if e["kind"] == "captured":
            s.update(ts=e["ts"], titular=p.get("headline"), region=p.get("region"))
        elif e["kind"] in ("triaged", "lowprio"):
            tr = p.get("triage", {})
            s.update(importancia=tr.get("importancia"), repercusion=tr.get("repercusion"))
    out = []
    for sid, s in by.items():
        imp = s.get("importancia")
        if not s.get("titular") or (imp is None and min_imp > 0) or (imp is not None and imp < min_imp):
            continue
        if bolsa:
            home = REGIONS.get(s.get("region") or "", (None, None))[1]
            if bolsa != home and bolsa not in ((s.get("repercusion") or {}).get("bolsas") or []):
                continue
        out.append((s["ts"], sid))
    return [sid for _, sid in sorted(out, reverse=True)[:60]]


def _fill_news(bus, n=300, seed=7):
    rnd = random.Random(seed)
    now = now_ms()
    for i, sec in enumerate(rnd.sample(range(60, 10 * 3600), n)):
        sid, t = f"n{i:04d}", now - sec * 1000
        bus.publish({"type": "event", "kind": "captured", "agent": "hunter", "story": sid, "ts": t, "title": f"t{i}",
                     "patch": {"headline": f"Titular {i}", "region": rnd.choice(["jp", "es", "us", "de", None]),
                               "tier": rnd.choice(["T0", "T1", "T2"]), "src": "x"}})
        for k in range(rnd.choice([0, 0, 1, 2])):
            bus.publish({"type": "event", "kind": rnd.choice(["triaged", "lowprio"]), "agent": "local", "story": sid,
                         "ts": t + 100 * (k + 1), "patch": {"triage": {
                             "importancia": rnd.randint(1, 9), "credibilidad": 70,
                             "repercusion": {"bolsas": rnd.sample(["tokio", "madrid", "londres"], rnd.randint(0, 2))}}}})
        if rnd.random() < 0.3:
            bus.publish({"type": "event", "kind": "corroborated", "agent": "hunter", "story": sid, "ts": t + 50,
                         "patch": {"corrob": [{}] * rnd.randint(1, 3)}})


def _ids(news):
    return [f"n{int(n['titular'].split()[1]):04d}" for n in news]


def test_noticias_eligen_lo_mismo_que_antes(tmp_path):
    store, bus, _, _ = setup(tmp_path)
    _fill_news(bus)
    for hours in (1, 3, 48):
        for min_imp in (0, 4, 7):
            for bolsa in (None, "madrid", "tokio"):
                got = recent_news(bus, hours, min_imp, bolsa)
                assert _ids(got) == _old_recent_news(bus, hours, min_imp, bolsa), (hours, min_imp, bolsa)
    got = recent_news(bus, 48, 0)
    assert len(got) == 60 and all("credibilidad_ajustada" in n for n in got)
    assert len(recent_news(bus, 48, 0, limit=75)) == 75   # hallazgo n.º 11: ?limite= (hasta 500)
    store.close()


async def test_api_noticias_acepta_limite(tmp_path):
    store, bus, _, _ = setup(tmp_path)
    _fill_news(bus, n=80)
    async with TestClient(TestServer(make_app(bus, lambda: {}))) as client:
        assert len(await (await client.get("/api/noticias?horas=48")).json()) == 60
        assert len(await (await client.get("/api/noticias?horas=48&limite=70")).json()) == 70
        assert len(await (await client.get("/api/noticias?horas=48&limite=9999")).json()) == 80   # tope 500
        assert (await client.get("/api/noticias?limite=x")).status == 400
    store.close()


async def test_api_noticias_en_un_hilo_sin_cargar_todo_en_el_bucle(tmp_path, monkeypatch):
    store, bus, _, _ = setup(tmp_path)
    _fill_news(bus, n=80)
    main = threading.get_ident()
    threads = []
    real = server.recent_news

    def spy(*a):
        threads.append(threading.get_ident())
        return real(*a)

    def no_range(*_a):
        raise AssertionError("no debe leer el rango entero con events_since")

    monkeypatch.setattr(server, "recent_news", spy)
    monkeypatch.setattr(store, "events_since", no_range)
    async with TestClient(TestServer(make_app(bus, lambda: {}))) as client:
        r = await client.get("/api/noticias?horas=48&min=6")
        got = await r.json()
    assert r.status == 200 and got and all(n["importancia"] >= 6 for n in got)
    assert threads and all(t != main for t in threads)
    store.close()


class _Broker:
    def __init__(self, extra=True):
        self.calls = []
        if extra:
            self.halt = lambda why: self.calls.append(("halt", why))
            self.resume = lambda why: self.calls.append(("resume", why))
            self.cancel = lambda story, why: self.calls.append(("cancel", story, why))

    def decide(self, story, ok, by=None):
        self.calls.append(("decide", story, ok))
        return True

    def close(self, story, why=""):
        self.calls.append(("close", story))

    def set_mode(self, mode):
        self.calls.append(("mode", mode))


async def _send_all(ws, msgs, broker, want):
    for m in msgs:
        await ws.send_str(m if isinstance(m, str) else json.dumps(m))
    for _ in range(100):
        if len(broker.calls) >= want:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.05)


async def test_websocket_solo_desde_la_propia_web(tmp_path):
    store, bus, _, _ = setup(tmp_path)
    async with TestClient(TestServer(make_app(bus, lambda: {}, Services(broker=_Broker())))) as client:
        for bad in ("http://evil.example", "null", f"http://127.0.0.1:{config.PORT + 1}", "http://localhost.evil.example"):
            with pytest.raises(aiohttp.WSServerHandshakeError) as e:
                await client.ws_connect("/events", origin=bad)
            assert e.value.status == 403, bad
        for good in (f"http://127.0.0.1:{config.PORT}", f"http://localhost:{config.PORT}"):
            ws = await client.ws_connect("/events", origin=good)
            assert json.loads((await ws.receive()).data)[0]["type"] == "reset"
            await ws.close()
    store.close()


async def test_ordenes_de_la_web_ok_estricto_y_nuevas(tmp_path):
    store, bus, _, _ = setup(tmp_path)
    b = _Broker()
    async with TestClient(TestServer(make_app(bus, lambda: {}, Services(broker=b)))) as client:
        ws = await client.ws_connect("/events", origin=f"http://127.0.0.1:{config.PORT}")
        await ws.receive()
        await _send_all(ws, [
            {"type": "decision", "story": "n1", "ok": "false"},   # la cadena no aprueba
            {"type": "decision", "story": "n1", "ok": 1},
            {"type": "decision", "story": "n1"},
            {"type": "decision", "story": ["n1"], "ok": True},
            {"type": "decision", "story": "x" * 500, "ok": True},
            {"type": "decision", "story": "n2", "ok": False},
            {"type": "decision", "story": "w-tokio-2026-10-26", "ok": True},
            {"type": "halt"}, {"type": "resume"}, {"type": "cancel", "story": "a-x-1"}, {"type": "cancel"},
            {"type": "mode", "mode": "veto"}, {"type": "mode", "mode": "yolo"}, {"type": "close", "story": "n3"},
        ], b, 7)
        await ws.close()
    assert b.calls == [("decide", "n2", False), ("decide", "w-tokio-2026-10-26", True), ("halt", "Detenida desde la web"),
                       ("resume", "Reanudada desde la web"), ("cancel", "a-x-1", "Cancelada por ti desde la web"),
                       ("mode", "veto"), ("close", "n3")]
    store.close()


async def test_ordenes_nuevas_sin_soporte_en_el_paper_no_rompen(tmp_path, caplog):
    store, bus, _, _ = setup(tmp_path)
    b = _Broker(extra=False)
    async with TestClient(TestServer(make_app(bus, lambda: {}, Services(broker=b)))) as client:
        ws = await client.ws_connect("/events")
        await ws.receive()
        await _send_all(ws, [{"type": "halt"}, {"type": "cancel", "story": "n1"}, {"type": "decision", "story": "n1", "ok": True}],
                        b, 1)
        await ws.close()
    assert b.calls == [("decide", "n1", True)]
    assert "aún no admite «halt»" in caplog.text and "aún no admite «cancel»" in caplog.text
    store.close()


async def test_web_lenta_se_desconecta_para_que_vuelva(tmp_path):
    store, bus, _, q0 = setup(tmp_path)
    bus.unsubscribe(q0)
    async with TestClient(TestServer(make_app(bus, lambda: {}))) as client:
        ws = await client.ws_connect("/events")
        await ws.receive()
        assert len(bus.clients) == 1
        for i in range(2100):   # sin ceder el bucle: la cola de esta web se llena
            bus.publish({"type": "market", "i": i})
        assert not bus.clients
        msg = await ws.receive(timeout=5)
        assert msg.type == WSMsgType.CLOSE and msg.data == WSCloseCode.TRY_AGAIN_LATER
    store.close()


def test_bus_avisa_a_la_web_lenta(tmp_path):
    store = Store(tmp_path / "b.db")
    bus = Bus(store)
    q = bus.subscribe()
    for i in range(q.maxsize + 1):
        bus.publish({"type": "market", "i": i})
    assert q not in bus.clients and q.qsize() == 1 and q.get_nowait() is None
    store.close()


async def test_al_apagar_se_cierran_las_webs_enseguida(tmp_path):
    store, bus, _, _ = setup(tmp_path)
    runner = web.AppRunner(make_app(bus, lambda: {}))
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    port = runner.addresses[0][1]
    async with aiohttp.ClientSession() as session:
        ws = await session.ws_connect(f"http://127.0.0.1:{port}/events")
        await ws.receive()
        t0 = time.monotonic()
        await asyncio.wait_for(runner.cleanup(), 10)   # antes esperaba 60 s con la web abierta
        assert time.monotonic() - t0 < 5
        msg = await ws.receive(timeout=5)
        assert msg.type == WSMsgType.CLOSE and msg.data == WSCloseCode.GOING_AWAY
    store.close()


def test_claves_vistas_en_hilo_escritor(tmp_path):
    path = tmp_path / "s.db"
    store = Store(path)
    assert not store.is_seen("a|1")
    store.mark_seen("a|1", 5)
    assert store.is_seen("a|1")   # al momento, aunque el hilo aún no la haya guardado
    store.flush()
    assert not store._pending and store.is_seen("a|1")
    assert store.db.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 0   # el checkpoint ya no va en el bucle
    con = sqlite3.connect(path)
    assert con.execute("SELECT ts FROM seen WHERE key='a|1'").fetchone() == (5,)
    con.close()
    for i in range(1200):
        store.mark_seen(f"k|{i}", i)
    store.close()   # guarda lo pendiente antes de cerrar
    store = Store(path)
    assert store.is_seen("k|1199") and store.db.execute("SELECT COUNT(*) FROM seen").fetchone() == (1201,)
    plan = store.db.execute("EXPLAIN QUERY PLAN SELECT rowid FROM seen WHERE ts < ? LIMIT 10", (100,)).fetchall()
    assert "seen_ts" in str(plan)   # la poda usa el índice y no recorre la tabla
    assert store.prune_seen(100, batch=30) == 30
    store.close()


async def test_poda_por_lotes_guarda_triajes_y_operaciones(tmp_path):
    store = Store(tmp_path / "p.db")
    bus = Bus(store)
    old = now_ms() - config.KEEP_EVENTS_S * 1000 - 3600_000
    for kind in ("captured", "corroborated", "verified", "flow", "triaged", "lowprio", "strategy", "order"):
        for i in range(3):
            bus.publish({"type": "event", "kind": kind, "agent": "x", "story": f"s{i}", "ts": old + i})
    bus.publish({"type": "event", "kind": "captured", "agent": "hunter", "story": "nueva", "ts": now_ms()})
    gone = await in_batches(functools.partial(store.prune, batch=2), config.PRUNE_EVENT_KINDS,
                            now_ms() - config.KEEP_EVENTS_S * 1000)
    assert gone == 12
    left = {k for (k,) in store.db.execute("SELECT DISTINCT kind FROM events")}
    assert left == {"captured", "triaged", "lowprio", "strategy", "order"}
    assert store.db.execute("SELECT story FROM events WHERE kind='captured'").fetchall() == [("nueva",)]
    store.close()


def test_indice_de_palabras_elige_igual_que_el_barrido(tmp_path):
    store, _, desk, _ = setup(tmp_path)
    rnd = random.Random(3)
    vocab = [f"pal{i}" for i in range(40)]
    found = 0
    for i in range(600):
        if i == 400:   # caduca la primera mitad: el índice debe olvidarla
            desk._expire(200 + config.DEDUPE_WINDOW_S * 1000)
            assert all(r.ts > 200 for q in desk._idx.values() for r in q)
        words = set(rnd.sample(vocab, rnd.randint(3, 7)))
        expected = next((r for r in desk.recent if similar(words, r.words)), None)
        assert desk._find_dup(words) is expected
        found += expected is not None
        if expected is None:
            desk._remember(Recent(i, f"s{i}", "x", "t", [], words))
    assert found > 50
    desk._expire(10_000 + config.DEDUPE_WINDOW_S * 1000)
    assert not desk.recent and not desk._idx
    store.close()


def test_percentil_cero_es_el_extremo_mas_fuerte():
    m = SimpleNamespace(flow={"BTC": {"pctl": 0.0}, "ETH": {"pctl": 80.36}, "SOL": {"pctl": None}, "XRP": {"pctl": 55.0}})
    assert Market._extremity(m, "BTC") == 50 and Market._extremity(m, "SOL") == 0
    assert max(config.COINS, key=lambda c: Market._extremity(m, c)) == "BTC"


async def test_api_vigilancia_con_y_sin_el_modulo(tmp_path):
    """/api/vigilancia: la vista del módulo (estado y operaciones virtuales); sin él, el último estado del bus o
    «apagada»."""
    store = Store(tmp_path / "t.db")
    bus = Bus(store)
    async with TestClient(TestServer(make_app(bus, lambda: {}))) as client:
        assert await (await client.get("/api/vigilancia")).json() == {"type": "vigilancia", "estado": "apagada",
                                                                        "estrategias": []}
        bus.publish({"type": "vigilancia", "estado": "ok", "estrategias": [{"codigo": "E1"}]})
        assert (await (await client.get("/api/vigilancia")).json())["estrategias"] == [{"codigo": "E1"}]
    vig = SimpleNamespace(view=lambda: {"type": "vigilancia", "estado": "ok", "estrategias": [], "operaciones": [{"id": "x"}]})
    async with TestClient(TestServer(make_app(bus, lambda: {}, Services(vigilancia=vig)))) as client:
        assert (await (await client.get("/api/vigilancia")).json())["operaciones"] == [{"id": "x"}]
    store.close()


# --- Integración: estado de las fuentes, aprendizaje, evolución y fuentes vivas ---

def _item(source, uid, title, url, t, estado=None, peso=None, outlet=None, mejora=None):
    it = Item(source, uid, title, url, t, outlet=outlet)
    if estado is not None:   # como lo marca feeds (fuentes_vivas)
        it.estado, it.peso, it.mejora = estado, peso, mejora
    return it


def test_mesa_pone_medio_estado_y_peso_de_la_fuente(tmp_path, monkeypatch):
    from bot import fuentes_vivas
    calls = []
    monkeypatch.setattr(fuentes_vivas, "prioridad_triaje", lambda prio, estado, story: calls.append((prio, estado, story))
                        or prio - 6)
    _, _, desk, q = setup(tmp_path)
    got = []
    desk.on_relevant = lambda *a, **k: got.append(k["prio"])
    t = now_ms() - 4000
    assert desk.handle(_item("coindesk", "a", "SEC approves spot Solana ETF with staking", "https://www.coindesk.com/x", t,
                             "silenciada", 0.0), CD, warm=False) == "captured"
    assert desk.handle(_item("theblock", "b", "SEC approves Solana spot ETF featuring staking", "https://www.theblock.co/y",
                             t), TB, warm=False) == "corroborated"
    cap, cor = events(q)
    p = cap["patch"]
    assert p["outlet"] == "coindesk.com" and p["srcState"] == "silenciada" and p["srcWeight"] == 0.0
    # La silenciada entra en la cola de la IA local con la prioridad de fuentes_vivas (muestreo o al final)
    [(prio, estado, story)] = calls
    assert estado == "silenciada" and story == cap["story"]
    c = cor["patch"]["corrob"][0]
    assert c["outlet"] == "theblock.co" and c["srcId"] == "theblock" and c["pub"] == t and c["srcWeight"] == 1.0
    # La confirma una fuente activa: la historia sube de estado, cuenta y vuelve al triaje con la prioridad de activa
    assert cor["patch"]["srcState"] == "activa" and cor["patch"]["srcWeight"] == 1.0
    assert got == [prio - 6, prio]
    # Otra confirmación activa: ya no sube nada ni se reenvía
    desk.handle(_item("decrypt", "d", "SEC approves Solana spot ETF with staking support", "https://decrypt.co/w", t),
                Source("decrypt", "Decrypt", "T2", 300, None), warm=False)
    assert "srcState" not in events(q)[0]["patch"] and got == [prio - 6, prio]
    # Una pieza de cazador (sin marca de feeds) va activa, con peso 1 y la prioridad de siempre
    desk.handle(Item("coindesk", "c", "Ether ETF inflows hit record", "https://www.coindesk.com/z", now_ms()), CD, warm=False)
    [ev] = events(q)
    assert ev["patch"]["srcState"] == "activa" and ev["patch"]["srcWeight"] == 1.0 and len(calls) == 1 and len(got) == 3


def test_mesa_retirada_no_se_tria_y_mejora_de_estado(tmp_path):
    _, _, desk, q = setup(tmp_path)
    got = []
    desk.on_relevant = lambda *a, **k: got.append(k["prio"])
    t = now_ms() - 4000
    GN = Source("jp-gn1", "GNews JP", "T2", 600, None)
    title = "Bitcoin ETF inflows hit record as Nikkei slides"
    assert desk.handle(_item("jp-gn1", "x", title, "https://news.google.com/a", t, "retirada", 0.0, outlet="nikkei.com"),
                       GN, warm=False) == "captured"
    assert got == []   # retirada: se publica para medirla, pero no gasta IA local
    from bot.desk import triage_priority
    rec = desk.recent[-1]
    base = rec.prio_base
    assert base is not None and rec.enviada is None
    # La misma entrada por otro feed sin mejor estado: nada; con mejora (la marca feeds), la historia sube de estado
    GN2 = Source("jp-gn2", "GNews JP 2", "T2", 600, None)
    assert desk.handle(_item("jp-gn2", "y", title, "https://news.google.com/b", t, "silenciada", 0.0, outlet="nikkei.com",
                             mejora="retirada"), GN2, warm=False) == "srcstate"
    assert desk.handle(_item("jp-gn2", "z", title, "https://news.google.com/c", t, "activa", 1.0, outlet="nikkei.com"),
                       GN2, warm=False) is None
    assert desk.handle(_item("jp-gn3", "w", title, "https://news.google.com/d", t, "activa", 1.0, outlet="nikkei.com",
                             mejora="silenciada"), GN2, warm=False) == "srcstate"
    cap, up1, up2 = events(q)
    assert cap["kind"] == "captured" and up1["kind"] == up2["kind"] == "srcstate" and up2["story"] == cap["story"]
    assert up1["patch"] == {"srcState": "silenciada", "srcWeight": 0.0}
    assert up2["patch"] == {"srcState": "activa", "srcWeight": 1.0} and "Sube a activa" in up2["text"]
    # Cada subida la manda al triaje si su prioridad nueva es mayor: retirada (nunca) → silenciada → activa
    sil = triage_priority(base, "silenciada", cap["story"])
    assert got[0] == sil and (got[1:] == [base] if base > sil else got[1:] == [])


def test_noticias_api_con_peso_del_medio_y_estado_de_la_fuente(tmp_path, monkeypatch):
    from bot import aprendizaje
    monkeypatch.setattr(aprendizaje, "_PESOS", {"bueno.com": 1.25})
    monkeypatch.setattr(aprendizaje, "_ALIAS", {})
    store, bus, _, _ = setup(tmp_path)
    t = now_ms() - 600_000

    def story(sid, url, estado="activa", peso=1.0, corrob=(), extra=()):
        bus.publish({"type": "event", "kind": "captured", "agent": "hunter", "story": sid, "ts": t, "title": sid,
                     "patch": {"headline": f"Titular {sid}", "src": "x", "tier": "T2", "url": url, "srcId": "f",
                               "outlet": url.split("/")[2].removeprefix("www."), "srcState": estado, "srcWeight": peso}})
        bus.publish({"type": "event", "kind": "triaged", "agent": "local", "story": sid, "ts": t + 1000,
                     "patch": {"triage": {"importancia": 7, "credibilidad": 60}}})
        if corrob:
            bus.publish({"type": "event", "kind": "corroborated", "agent": "hunter", "story": sid, "ts": t + 2000,
                         "patch": {"corrob": list(corrob)}})
        for e in extra:
            bus.publish({"type": "event", "agent": "hunter", "story": sid, "ts": t + 3000, **e})

    story("s1", "https://www.bueno.com/a")
    story("s2", "https://normal.org/b", "silenciada", 0.0, corrob=[{"srcWeight": 0.0}, {"srcWeight": 1.0}, {}])
    story("s3", "https://otro.net/c", "silenciada", 0.0,
          extra=[{"kind": "srcstate", "patch": {"srcState": "prueba", "srcWeight": 0.5}}])
    news = {n["titular"].split()[1]: n for n in recent_news(bus, 1, 0)}
    s1, s2, s3 = news["s1"], news["s2"], news["s3"]
    assert s1["medio"] == "bueno.com" and s1["peso_medio"] == 1.25 and s1["credibilidad_ajustada"] == 75   # 60 × 1,25
    # Silenciada: no cuenta; las confirmaciones de fuentes silenciadas tampoco son independientes
    assert s2["estado_fuente"] == "silenciada" and s2["credibilidad_ajustada"] == 0 and s2["confirmaciones"] == 2
    assert s3["estado_fuente"] == "prueba" and s3["peso_fuente"] == 0.5 and s3["credibilidad_ajustada"] == 30
    store.close()


def test_funding_con_su_periodo_de_binance(tmp_path):
    from bot.market import funding_8h, funding_hours
    hours = funding_hours([{"symbol": "BTCUSDT", "fundingIntervalHours": 4}, {"symbol": "DOGEUSDT"}, "basura"],
                          config.COINS)
    assert hours == {"BTC": 4.0, "ETH": 8.0, "SOL": 8.0, "XRP": 8.0}
    assert funding_8h({"funding": 0.01, "funding_h": 4.0}) == pytest.approx(0.02)
    assert funding_8h({"funding": 0.01}) == 0.01
    assert funding_hours(None, ["BTC"]) == {"BTC": 8.0}
    store = Store(tmp_path / "t.db")
    bus = Bus(store)
    bus.publish({"type": "market", "prices": {}, "flow": {"BTC": {"ls": 1.2, "funding": 0.01, "funding_h": 4.0, "oi": 0.1,
                                                                  "taker": 0.5, "pctl": 50}}})
    f = server.state_summary(bus)["flujo_minorista"]["BTC"]
    assert f["funding_pct_8h"] == pytest.approx(0.02) and f["funding_periodo_h"] == 4.0
    store.close()


def _modules(tmp_path, bus, store):
    from bot.aprendizaje import Aprendizaje
    from bot.evolucion import Evolucion
    from bot.fuentes_vivas import FuentesVivas
    fv = FuentesVivas(bus, store, None, registro_dir=tmp_path / "fuentes", auto=False, descubrir=False)
    apr = Aprendizaje(bus, store)
    evo = Evolucion(bus, store, archivo=tmp_path / "estrategia_activa.json", n_boot=50)
    evo.conectar_fuentes(fv)
    evo.conectar_aprendizaje(apr)
    return fv, apr, evo


async def test_api_aprendizaje_evolucion_y_fuentes_sin_los_modulos(tmp_path):
    store = Store(tmp_path / "t.db")
    bus = Bus(store)
    async with TestClient(TestServer(make_app(bus, lambda: {}))) as client:
        apr = await (await client.get("/api/aprendizaje")).json()
        evo = await (await client.get("/api/evolucion")).json()
        fv = await (await client.get("/api/fuentes_vivas")).json()
        assert apr["estado"] == "apagado" and evo["estado"] == "apagada" and fv["estado"] == "apagado"
        for path in ("/api/aprendizaje/medios", "/api/aprendizaje/cambios", "/api/evolucion/historial",
                     "/api/evolucion/propuestas"):
            assert (await client.get(path)).status == 404, path
        assert await (await client.get("/api/fuentes_vivas/lista")).json() == []
        bus.publish({"type": "aprendizaje", "estado": "ok", "medios_mejores": []})
        assert (await (await client.get("/api/aprendizaje")).json())["estado"] == "ok"   # el último estado del bus
    store.close()


async def test_api_aprendizaje_evolucion_y_fuentes_con_los_modulos_reales(tmp_path):
    from bot.feeds import FeedPoller, FeedSpec
    store = Store(tmp_path / "t.db")
    bus = Bus(store)
    feeds = FeedPoller(bus, NewsDesk(bus, store), None, store,
                       [FeedSpec("jp-a", "A", "https://a.example/rss", "ja", "JP", "jp", "rss", "T2"),
                        FeedSpec("jp-b", "B", "https://b.example/rss", "ja", "JP", "jp", "rss", "T2")])
    fv, apr, evo = _modules(tmp_path, bus, store)
    fv.attach(feeds)
    assert fv.silenciar("jp-b", "ruido")["ok"]
    sv = Services(feeds=feeds, aprendizaje=apr, evolucion=evo, fuentes_vivas=fv)
    async with TestClient(TestServer(make_app(bus, lambda: {}, sv))) as client:
        async def get(path):
            r = await client.get(path)
            assert r.status == 200, path
            return await r.json()

        r = await get("/api/fuentes_vivas")
        assert r["type"] == "fuentes_vivas" and r["feeds"]["silenciada"] == 1 and r["aviso"]
        assert r["lista"][0]["id"] == "jp-b" and r["candidatos"] == []
        assert [x["id"] for x in await get("/api/fuentes?estado=silenciada")] == ["jp-b"]
        assert [x["id"] for x in await get("/api/fuentes_vivas/lista?estado=activa&tipo=feed")] == ["jp-a"]
        hist = await get("/api/fuentes_vivas/historial?id=jp-b")
        assert hist[0]["despues"] == "silenciada" and hist[0]["deshacer"]["accion"] == "restaurar"
        assert await get("/api/fuentes_vivas/candidatos") == []
        a = await get("/api/aprendizaje")
        assert a["type"] == "aprendizaje" and a["noticias"] == {"mejores": [], "peores": []}
        assert {"medios", "postmortems", "cambios", "aviso"} <= set(a)
        assert await get("/api/aprendizaje/medios?orden=casos&limit=5") == []
        assert await get("/api/aprendizaje/estadisticas?por=region") == []
        assert await get("/api/aprendizaje/noticias?cuales=peores&horizonte=60") == []
        assert await get("/api/aprendizaje/postmortems?origen=vigilancia") == []
        assert await get("/api/aprendizaje/cambios?medio=x.com") == []
        assert (await client.get("/api/aprendizaje/medio?id=nadie.com")).status == 404
        for bad in ("/api/aprendizaje/medios?orden=nada", "/api/aprendizaje/estadisticas?por=x",
                    "/api/aprendizaje/noticias?horizonte=7", "/api/aprendizaje/postmortems?origen=real",
                    "/api/aprendizaje/medios?limit=mucho", "/api/aprendizaje/medio", "/api/fuentes_vivas/lista?tipo=x",
                    "/api/evolucion/informes?semana=ayer", "/api/evolucion/historial?desde=ayer"):
            assert (await client.get(bad)).status == 400, bad
        e = await get("/api/evolucion")
        assert e["type"] == "evolucion" and e["modo"] == "proponer" and e["informe_semanal"] is None and e["aviso"]
        h = await get("/api/evolucion/historial?tipo=fuente_silenciada,fuente_restaurada")
        assert [x["tipo"] for x in h["cambios"]] == ["fuente_silenciada"] and h["tipos"]
        assert await get("/api/evolucion/informes") == {"informes": []}
        assert await get("/api/evolucion/propuestas?estado=pendiente") == {"propuestas": []}
    store.close()


async def _until(cond, tries=150):
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_ordenes_web_de_fuentes_pesos_y_evolucion_quedan_en_el_historial(tmp_path, monkeypatch):
    from bot import aprendizaje, evolucion
    monkeypatch.setattr(aprendizaje, "_PESOS", {})
    monkeypatch.setattr(aprendizaje, "_ALIAS", {})
    store = Store(tmp_path / "t.db")
    bus = Bus(store)
    fv, apr, evo = _modules(tmp_path, bus, store)
    sv = Services(aprendizaje=apr, evolucion=evo, fuentes_vivas=fv)   # sin paper: estas órdenes van igual
    pid = "p-prueba-1"
    store.db.execute("INSERT INTO evolution_proposals VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (pid, now_ms(), "parametro_cambiado", "ventana china", "quitar:china", "pendiente",
                      json.dumps({"titulo": "Quitar la ventana de China"}), now_ms(), None, None, None))
    # ejemplo.com ya se ha visto (una noticia captada); inventado.invalid no
    store.db.execute("INSERT INTO fv_noticias (story, ts, feed, medio) VALUES ('n1', ?, 'gn-es', 'ejemplo.com')", (now_ms(),))
    store.db.commit()
    tipos = lambda: [e["tipo"] for e in evolucion.leer(store, limite=50)]
    async with TestClient(TestServer(make_app(bus, lambda: {}, sv))) as client:
        ws = await client.ws_connect("/events", origin=f"http://127.0.0.1:{config.PORT}")
        await ws.receive()
        for bad in ({"type": "fuente", "accion": "borrar", "id": "medio:x.com"}, {"type": "fuente", "accion": "silenciar"},
                    {"type": "fuente", "accion": "silenciar", "id": "m" * 301},
                    {"type": "peso_medio", "medio": "x.com", "peso": "alto"},
                    {"type": "peso_medio", "medio": "x.com", "peso": True}, {"type": "revertir_peso", "id": "uno"}):
            await ws.send_str(json.dumps(bad))
        await ws.send_str(json.dumps({"type": "fuente", "accion": "silenciar", "id": "medio:ejemplo.com",
                                      "motivo": "  solo   ruido "}))
        assert await _until(lambda: "fuente_silenciada" in tipos())
        assert fv.filas["medio:ejemplo.com"].estado == "silenciada" and bus.state["orden_web"]["ok"]
        e = evolucion.leer(store, tipo="fuente_silenciada")[0]
        assert e["origen"] == "fuentes_vivas" and "solo ruido" in e["motivo"]
        await ws.send_str(json.dumps({"type": "fuente", "accion": "activar", "id": "nadie"}))   # no existe
        assert await _until(lambda: bus.state["orden_web"]["ok"] is False)
        assert bus.state["orden_web"]["why"] == "No conozco esa fuente"
        # Un medio que nadie ha visto (o que no es un dominio) no crea una fuente silenciada inventada
        for fid in ("medio:inventado.invalid", "medio:no es un dominio", "medio:x"):
            n = len(tipos())
            await ws.send_str(json.dumps({"type": "fuente", "accion": "silenciar", "id": fid, "motivo": "prueba"}))
            assert await _until(lambda: bus.state["orden_web"]["id"] == fid)
            assert bus.state["orden_web"]["ok"] is False and bus.state["orden_web"]["why"] == "No conozco esa fuente"
            assert fid not in fv.filas and len(tipos()) == n
        # Peso de un medio a mano y deshacerlo: lo guarda el aprendizaje y la evolución lo copia a su historial
        await ws.send_str(json.dumps({"type": "peso_medio", "medio": "ejemplo.com", "peso": 1.3, "motivo": "lo sé"}))
        assert await _until(lambda: aprendizaje.source_weight("ejemplo.com") == 1.3)
        assert bus.state["orden_web"]["id"] == "ejemplo.com"
        hist = await apr.cambios(10)
        await ws.send_str(json.dumps({"cmd": "aprendizaje_revertir", "id": hist[0]["id"]}))
        assert await _until(lambda: aprendizaje.source_weight("ejemplo.com") == 1.0)
        assert apr.weights["ejemplo.com"]["manual"] is None   # deshacer lo fijado a mano: vuelve al automático
        assert await evo.sincronizar_aprendizaje() == 2 and tipos().count("peso_cambiado") == 2
        rev, orig = evolucion.leer(store, tipo="peso_cambiado")   # la reversión queda enlazada con lo que deshace
        assert rev["revierte"] == orig["id"] and orig["revertida_por"] == rev["id"] and "n.º" in rev["motivo"]
        vista = (await evo.api_historial(tipo="peso_cambiado"))["cambios"]
        assert [x["reversible"] for x in vista] == [True, False]   # lo ya revertido no se ofrece
        # Evolución: modo, aplicar algo que no existe y descartar una propuesta (también queda en el historial)
        await ws.send_str(json.dumps({"type": "evolucion", "accion": "modo", "modo": "automatico"}))
        assert await _until(lambda: evo.modo == "automatico")
        await ws.send_str(json.dumps({"type": "evolucion", "accion": "aplicar", "id": "p-nada"}))
        assert await _until(lambda: (evo.ultima_orden or {}).get("accion") == "aplicar")
        assert evo.ultima_orden["ok"] is False
        await ws.send_str(json.dumps({"type": "evolucion", "accion": "descartar", "id": pid, "motivo": "no me fío"}))
        assert await _until(lambda: "propuesta_descartada" in tipos())
        await ws.send_str(json.dumps({"type": "evolucion", "accion": "borrar_todo"}))
        await ws.close()
    d = evolucion.leer(store, tipo="propuesta_descartada")[0]
    assert d["origen"] == "web" and "Quitar la ventana de China" in d["motivo"] and "no me fío" in d["motivo"]
    assert (await evo.api_propuestas("descartada"))["propuestas"][0]["id"] == pid
    assert "parametro_cambiado" in tipos()   # el cambio de modo
    store.close()


def test_arranque_temporales_en_m_gancho_del_bus_y_modulos_opcionales(tmp_path, monkeypatch):
    import os
    import tempfile
    from bot import __main__ as main
    for var in ("TMP", "TEMP", "SQLITE_TMPDIR"):
        monkeypatch.setenv(var, os.environ.get(var, ""))   # se restauran al acabar
    monkeypatch.setattr(tempfile, "tempdir", tempfile.tempdir)
    monkeypatch.setattr(config, "TMP_DIR", tmp_path / "tmp")
    main.data_tmp()
    assert os.environ["TMP"] == os.environ["SQLITE_TMPDIR"] == str(tmp_path / "tmp") and (tmp_path / "tmp").is_dir()
    assert main.optional("Uno que falla", lambda: 1 / 0) is None and main.optional("Otro", lambda: 5) == 5
    store = Store(tmp_path / "t.db")
    bus = Bus(store)
    seen = []
    main.observe_bus(bus, seen.append)
    m = bus.publish({"type": "market", "prices": {}})
    assert seen == [m] and bus.state["market"] is m
    store.close()


async def test_supervise_relanza_un_bucle_caido_sin_parar_al_resto(caplog):
    """Hallazgo n.º 46: un fallo suelto en un bucle ya no tumba el proceso: se registra y se relanza con espera
    creciente; si termina sin error, no se relanza; y la cancelación (apagado) se respeta."""
    import asyncio
    from bot import __main__ as main
    caplog.set_level("INFO")
    calls, waits = [], []

    async def cae_dos_veces():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("database is locked")

    real_sleep = asyncio.sleep

    async def fake_sleep(s):
        waits.append(s)
        await real_sleep(0)

    main.asyncio.sleep = fake_sleep
    try:
        await main.supervise("Prueba", cae_dos_veces, min_s=5, max_s=300)
    finally:
        main.asyncio.sleep = real_sleep
    assert len(calls) == 3 and waits == [5, 10] and "Prueba se cayó" in caplog.text and "Prueba terminó" in caplog.text
    # el resto sigue: otra tarea a la vez no se entera de los fallos
    otra = []

    async def bien():
        otra.append(1)

    async def siempre_falla():
        raise ValueError("x")

    t = asyncio.ensure_future(main.supervise("Mala", siempre_falla, min_s=0.01, max_s=0.01))
    await asyncio.gather(main.supervise("Buena", bien), real_sleep(0.05))
    assert otra == [1] and not t.done()
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t


def test_store_espera_30_s_si_la_base_esta_ocupada(tmp_path):
    """Hallazgo n.º 46: la conexión principal espera hasta 30 s a un bloqueo de otra conexión (antes, 5 s)."""
    import sqlite3
    store = Store(tmp_path / "t.db")
    other = sqlite3.connect(tmp_path / "t.db", timeout=0)
    other.execute("BEGIN IMMEDIATE")
    t0 = time.monotonic()
    with pytest.raises(sqlite3.OperationalError):
        other2 = sqlite3.connect(tmp_path / "t.db", timeout=0.2)
        other2.execute("BEGIN IMMEDIATE")
    assert time.monotonic() - t0 < 5
    other.rollback()
    store.db.execute("BEGIN IMMEDIATE")
    store.db.rollback()
    assert store.db.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    other.close()
    store.close()


def ms_utc(*a) -> int:
    from datetime import datetime, timezone
    return int(datetime(*a, tzinfo=timezone.utc).timestamp() * 1000)


def test_cripto_va_antes_que_la_macro_en_la_cola_de_la_ia_local():
    """Hallazgo n.º 7 de la revisión: la macro es unas 2,6 veces más numerosa; la cripto del mismo nivel y frescura va
    un punto por delante (la cola descarta lo menos prioritario y más antiguo)."""
    from bot.desk import priority
    from bot.filters import classify
    cripto = classify("Bitcoin ETF sees record inflows", "x", 60, lang="en")
    macro = classify("Russia launches missiles at Kyiv", "x", 60, lang="en")
    contexto = classify("Apple unveils new iPhone with AI features", "x", 60, lang="en")
    assert cripto.clase == "cripto" and macro.clase == "macro" and contexto.clase == "contexto"
    lejos = ms_utc(2026, 10, 28, 19, 0)   # ninguna bolsa de Asia abre en 3 h ni abrió hace menos de 4 h
    for tier in ("T0", "T1", "T2"):
        assert priority(cripto, tier) > priority(macro, "T0", "jp", lejos) + 1 > priority(contexto, tier)
    viejo = classify("Bitcoin ETF sees record inflows", "x", 5 * 3600, lang="en")   # de hace horas
    # n.º 1 y 42: la cripto, siempre antes que la macro (con la cola llena se pierde macro, no cripto)
    assert priority(viejo, "T2") > priority(macro, "T0", "jp", ms_utc(2026, 10, 28, 23, 30)) > priority(macro, "T0")
    # la macro de la región cuya bolsa abre pronto (Tokio abre a las 00:00 UTC) o abrió hace poco, un punto por delante
    assert priority(macro, "T1", "jp", ms_utc(2026, 10, 28, 22, 0)) == priority(macro, "T1", "jp", lejos) + 1
    assert priority(macro, "T1", "jp", ms_utc(2026, 10, 29, 3, 0)) == priority(macro, "T1", "jp", lejos) + 1
    assert priority(macro, "T1", "global", ms_utc(2026, 10, 28, 22, 0)) == priority(macro, "T1", "jp", lejos)


def test_api_estado_resume_la_cripto_en_las_aperturas_y_no_el_indice(tmp_path):
    """Hallazgo n.º 7: /api/estado ya no devuelve la variación de cada índice (bolsas.cambio_pct), sino cómo va la
    cripto en cada apertura (del último «cripto_aperturas»)."""
    from bot.bus import Bus
    from bot.store import Store
    store = Store(tmp_path / "t.db")
    bus = Bus(store)
    bus.publish({"type": "trend", "markets": {"tokio": {"state": "abierta", "change_pct": 1.2, "index": "Nikkei 225"}}})
    st = server.state_summary(bus)
    assert "bolsas" not in st and st["aperturas_cripto"] == {}
    bus.publish({"type": "cripto_aperturas", "ts": 1, "coins": ["BTC", "ETH"], "markets": {
        "tokio": {"name": "Tokio", "state": "en_curso", "open_ms": 1_793_000_000_000, "minutes_since_open": 70,
                  "coins": {"BTC": {"ret_pct": 0.4, "range_pct": 1.1}, "ETH": {"ret_pct": -0.2, "range_pct": 1.8}}},
        "nuevayork": {"name": "Nueva York", "state": "por_abrir", "open_ms": None, "coins": None,
                      "last": {"open_ms": 1_792_900_000_000, "coins": {"BTC": {"ret_4h_pct": 0.6, "range_4h_pct": 1.9}}}},
        "dubai": {"name": "Dubái", "state": "por_abrir", "coins": None, "last": None}}})
    ap = server.state_summary(bus)["aperturas_cripto"]
    tk, ny, du = ap["tokio"], ap["nuevayork"], ap["dubai"]
    assert tk["estado"] == "en_curso" and tk["medida"] == "desde la apertura" and tk["media_pct"] == pytest.approx(0.1)
    assert tk["cripto"]["BTC"] == {"pct": 0.4, "rango_pct": 1.1} and tk["apertura"] and tk["minutos_desde_apertura"] == 70
    assert ny["medida"].startswith("última apertura") and ny["cripto"]["BTC"]["pct"] == 0.6 and ny["apertura"]
    assert du["cripto"] is None and du["media_pct"] is None and du["apertura"] is None
    json.dumps(ap)
    store.close()



def test_copia_diaria_en_caliente_comprobada_y_7_copias(tmp_path):
    """Hallazgo n.º 39: copia en caliente con la API backup de sqlite3 (con el bot escribiendo a la vez), comprobada y
    renombrada al final; se conservan 7."""
    from bot.store import copia_diaria
    store = Store(tmp_path / "bot.db")
    for k in range(50):
        store.add_event({"id": f"e{k}", "ts": k, "kind": "triaged", "story": f"s{k}", "agent": "local"})
    otra = sqlite3.connect(tmp_path / "bot.db")
    otra.execute("BEGIN IMMEDIATE")   # alguien escribiendo a la vez: la copia no espera ni falla
    otra.execute("INSERT INTO events VALUES ('x', 99, 'sx', 'local', 'triaged', '{}')")
    carpeta = tmp_path / "copias"
    dest = copia_diaria(tmp_path / "bot.db", carpeta, "2026-10-28")
    otra.commit()
    otra.close()
    assert dest.name == "bot-2026-10-28.db" and not list(carpeta.glob("*.tmp"))
    c = sqlite3.connect(dest)
    assert c.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 50 and c.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    c.close()
    assert copia_diaria(tmp_path / "bot.db", carpeta, "2026-10-28") is None   # la de ese día ya está
    for d in range(1, 10):
        copia_diaria(tmp_path / "bot.db", carpeta, f"2026-11-{d:02d}")
    names = sorted(p.name for p in carpeta.glob("bot-*.db"))
    assert len(names) == 7 and names[0] == "bot-2026-11-03.db" and names[-1] == "bot-2026-11-09.db"
    from bot import __main__ as main
    h = main.db_health(tmp_path / "bot.db", carpeta)
    assert h["copias"] == 7 and h["ultima_copia"] == "bot-2026-11-09.db" and h["mb"] is not None
    store.close()


async def test_limpieza_poda_las_captadas_a_los_7_dias_y_hace_la_copia(tmp_path, monkeypatch):
    """Hallazgos n.º 51 y 39: las captadas, confirmaciones y subidas de estado se podan a los 7 días (antes 30); los
    triajes nunca. La copia diaria se hace en la misma ronda."""
    import asyncio
    from bot import __main__ as main
    store = Store(tmp_path / "bot.db")
    now = now_ms()
    for k, (kind, dias) in enumerate([("captured", 8), ("captured", 6), ("corroborated", 8), ("triaged", 40),
                                      ("verified", 8), ("verified", 31)]):
        store.add_event({"id": f"e{k}", "ts": now - dias * 86_400_000, "kind": kind, "story": f"s{k}", "agent": "x"})
    monkeypatch.setattr(config, "COPIAS_DIR", tmp_path / "copias")

    real_sleep = asyncio.sleep

    async def para(s):   # la espera de una hora corta la prueba; las de 0 s entre lotes, no
        if s >= 3600:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(main.asyncio, "sleep", para)
    with pytest.raises(asyncio.CancelledError):
        await main.maintenance(store)
    left = sorted((e["kind"], e["id"]) for e in store.events_since(0, ("captured", "corroborated", "triaged", "verified")))
    assert left == [("captured", "e1"), ("triaged", "e3"), ("verified", "e4")]
    assert len(list((tmp_path / "copias").glob("bot-*.db"))) == 1
    store.close()


def test_llama_server_log_rota_al_pasar_de_50_mb(tmp_path):
    """Hallazgo n.º 50: llama-server.log se abría en «ab» en cada arranque y crecía sin límite."""
    from bot.local_ai import rotate_log
    log_ = tmp_path / "llama-server.log"
    log_.write_bytes(b"x" * 2_000)
    assert rotate_log(log_, 0.001) and not log_.exists() and (tmp_path / "llama-server.log.1").stat().st_size == 2_000
    log_.write_bytes(b"y" * 10)
    assert not rotate_log(log_, 0.001) and log_.exists()
    assert not rotate_log(tmp_path / "no-existe.log", 1)


def test_foto_inicial_lleva_los_totales_reales(tmp_path):
    """Hallazgo n.º 8: la foto trae 1.500 eventos y 300 descartes; el mensaje «conteo» (al final) da los totales reales
    desde el arranque para que la web no muestre una proporción falsa de captadas frente a ruido."""
    store = Store(tmp_path / "t.db")
    bus = Bus(store)
    t = bus.started
    for k in range(5):
        bus.publish({"type": "event", "kind": "captured", "story": f"c{k}", "ts": t + k})
    for k in range(12):
        bus.publish({"type": "event", "kind": "filtered", "story": f"f{k}", "ts": t + k})
    bus.publish({"type": "event", "kind": "captured", "story": "viejo", "ts": t - 3_600_000})   # antes del arranque
    snap = bus.snapshot()
    assert snap[-1] == {"type": "conteo", "desde": t, "kinds": {"captured": 5, "filtered": 12, "corroborated": 0,
                                                               "triaged": 0, "lowprio": 0}}
    assert bus.conteo(t + 30 * 3_600_000)["desde"] == t + 6 * 3_600_000   # 24 h como mucho
    store.close()
