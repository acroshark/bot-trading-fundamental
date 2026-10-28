"""Fuentes masivas: planificador con reloj falso, GET condicional, espaciado por host, registro, importador y resumen."""
import asyncio
import itertools
import json
import re
import time
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bot.bus import Bus
from bot.desk import NewsDesk
from bot import feeds
from bot.feeds import (Delivered, FeedPoller, FeedSpec, FeedState, HostGate, Scheduler, entry_key, load_registry,
                       norm_url, short_error, site)
from bot.importar_fuentes import importar, main as importar_main
from bot.models import Item, now_ms
from bot.store import Store

# Titulares recortados de los RSS de NHK y Nikkei del 23-10-2026; las fechas se generan recientes para que no
# cuenten como histórico del arranque.
TITLES = ["日銀、金融政策決定会合で利上げ見送り 植田総裁が会見", "日経平均、一時500円超安 米中摩擦の再燃を警戒",
          "円相場、1ドル=152円台に下落 日米金利差を意識", "ビットコイン急伸、米国でETF資金流入が拡大"]


def rss(titles=TITLES, start=0, google=False) -> bytes:
    now = datetime.now(timezone.utc)
    items = "".join(
        f"<item><title>{t}{' - 日本経済新聞' if google else ''}</title><link>https://www.nikkei.com/article/{start + i}</link>"
        f"<guid>k{start + i}</guid><pubDate>{format_datetime(now - timedelta(minutes=2 + i))}</pubDate></item>"
        for i, t in enumerate(titles))
    return (f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>NHKニュース</title>'
            f"<link>https://www3.nhk.or.jp/news/</link>{items}</channel></rss>").encode()


def spec(fid="jp-nhk-a1b2c3", url="https://www3.nhk.or.jp/rss/news/cat0.xml", region="jp", tier="T1", kind="rss",
         interval_s=None, lang="ja") -> FeedSpec:
    return FeedSpec(fid, fid.upper(), url, lang, "JP", region, kind, tier, interval_s)


class FakeDesk:
    def __init__(self, results=("captured",)):
        self.results = itertools.cycle(results)
        self.calls = []

    def handle(self, item, src, warm):
        self.calls.append((item, src, warm))
        return next(self.results)


def make(tmp_path, specs, session=None, desk=None):
    store = Store(tmp_path / "t.db")
    bus = Bus(store)
    p = FeedPoller(bus, desk or FakeDesk(), session, store, specs)
    p.parse_workers = 0   # en las pruebas se lee en un hilo: lanzar procesos tarda segundos en Windows
    return p, bus, store


# ---------- Intervalos y planificador ----------
def test_intervalo_base_por_nivel_y_google_news():
    assert [FeedState(spec(tier=t)).base for t in ("T0", "T1", "T2", "T3")] == [90, 120, 180, 300]
    assert FeedState(spec(tier="T0", kind="google_news")).base == 600
    assert FeedState(spec(tier="T3", interval_s=45)).base == 45


def test_intervalo_adaptativo_y_espera_por_errores():
    st = FeedState(spec(tier="T2"))
    seq = []
    for _ in range(18):
        st.on_success(0)
        seq.append(st.interval)
    assert seq[:3] == [180, 180, 270] and seq[5] == 405 and seq[8] == pytest.approx(607.5)
    assert seq[-1] == 900   # tope
    st.on_success(2)        # con novedades vuelve a la base
    assert st.interval == 180 and st.empty_polls == 0
    waits = []
    for _ in range(6):
        st.on_error("HTTP 503")
        waits.append(st.next_wait())
    assert waits == [360, 720, 1440, 1800, 1800, 1800] and st.ok is False and st.view()["err"] == "HTTP 503"
    st.on_success(1)
    assert st.fails == 0 and st.next_wait() == 180 and st.ok


def test_heap_saca_por_hora_de_sondeo():
    s = Scheduler()
    for fid, due in (("c", 5.0), ("a", 1.0), ("b", 3.0), ("d", 3.0)):
        s.push(fid, due)
    assert s.pop_due(0.5) is None and s.next_due() == 1.0
    assert [s.pop_due(10) for _ in range(4)] == ["a", "b", "d", "c"]   # a igual hora, por orden de llegada
    assert s.pop_due(10) is None and len(s) == 0


def test_espaciado_por_host_con_reloj_falso():
    g = HostGate()
    g.mark("news.google.com", 100.0)
    g.mark("www3.nhk.or.jp", 100.0)
    assert g.delay("news.google.com", 100.0) == pytest.approx(1.2)
    assert g.delay("news.google.com", 101.0) == pytest.approx(0.2)
    assert g.delay("www3.nhk.or.jp", 100.1) == pytest.approx(0.2)
    assert g.delay("www.nikkei.com", 100.0) == 0   # cada host va por su lado


async def test_planificador_con_reloj_falso(tmp_path):
    specs = [spec("a", "https://a.example/rss", tier="T0"), spec("b", "https://b.example/rss", tier="T3"),
             spec("g1", "https://news.google.com/rss/search?q=nikkei&ceid=JP:ja", kind="google_news"),
             spec("g2", "https://news.google.com/rss/search?q=boj&ceid=JP:ja", kind="google_news")]
    p, bus, store = make(tmp_path, specs)
    t = [0.0]
    polls: dict[str, list[float]] = {}

    async def fake_get(st):
        polls.setdefault(st.spec.id, []).append(t[0])
        return 304, b"", st.etag, st.last_modified

    async def fake_sleep(d):
        for _ in range(3):       # deja correr los sondeos en marcha antes de avanzar el reloj
            await asyncio.sleep(0)
        t[0] += d
        if t[0] > 700:
            p.close()

    p._get, p.clock, p.sleep, p.jitter, p.start_spread_s = fake_get, lambda: t[0], fake_sleep, 0, 0
    await asyncio.wait_for(p.run(), 10)
    assert polls["a"] == pytest.approx([0, 90, 180, 315, 450, 585])   # tras 3 sondeos vacíos: ×1,5
    assert polls["b"] == pytest.approx([0, 300, 600])
    assert polls["g1"] == pytest.approx([0, 600], abs=0.02)
    assert polls["g2"] == pytest.approx([1.2, 601.2], abs=0.02)       # 1,2 s entre peticiones a Google News
    assert p.states["a"].interval == pytest.approx(202.5) and p.states["b"].interval == 450
    assert bus.state["sources_summary"]["totals"]["feeds"] == 4
    row = store.db.execute("SELECT interval_s, fails FROM feed_state WHERE id='a'").fetchone()
    assert row == (pytest.approx(202.5), 0)   # close() guarda el estado
    store.close()


# ---------- Peticiones reales contra un servidor local ----------
def feed_app(handlers: dict) -> web.Application:
    app = web.Application()
    for path, h in handlers.items():
        app.router.add_get(path, h)
    return app


async def test_get_condicional_etag_304_y_reinicio(tmp_path):
    seen, version = [], {"v": 1}

    async def handler(req):
        seen.append((req.headers.get("If-None-Match"), req.headers.get("If-Modified-Since"),
                     req.headers.get("User-Agent")))
        etag = f'"v{version["v"]}"'
        if req.headers.get("If-None-Match") == etag:
            return web.Response(status=304)
        body = rss() if version["v"] == 1 else rss(["日銀総裁が臨時会見"], start=10)
        return web.Response(body=body, content_type="application/rss+xml",
                            headers={"ETag": etag, "Last-Modified": "Fri, 23 Oct 2026 08:00:00 GMT"})

    async with TestServer(feed_app({"/rss": handler})) as server, aiohttp.ClientSession() as session:
        sp = spec(url=str(server.make_url("/rss")))
        desk = FakeDesk(("captured", "filtered", None, "corroborated"))
        p, bus, store = make(tmp_path, [sp], session, desk)
        st = p.states[sp.id]

        await p.poll(st)
        assert len(desk.calls) == 4 and all(warm for *_, warm in desk.calls)   # primer sondeo: histórico sin inundar
        item, src = desk.calls[0][:2]
        assert (item.region, item.lang, src.id, src.tier, src.interval, src.fetch) == ("jp", "ja", sp.id, "T1", 120, None)
        assert st.ok and st.etag == '"v1"' and st.items_total == 3
        v = st.view()
        assert (v["items_last_hour"], v["relevant_last_hour"], v["median_lat_s"]) == (3, 1, None)
        assert "Mozilla/5.0" in seen[0][2] and "BotFundamental/0.1" in seen[0][2] and seen[0][0] is None

        await p.poll(st)   # sin cambios: 304, la mesa no recibe nada
        assert seen[1][:2] == ('"v1"', "Fri, 23 Oct 2026 08:00:00 GMT")
        assert len(desk.calls) == 4 and st.ok and st.empty_polls == 1
        p.close()
        assert store.db.execute("SELECT etag, fails FROM feed_state WHERE id=?", (sp.id,)).fetchone() == ('"v1"', 0)

        p2 = FeedPoller(bus, desk, session, store, [sp])   # reinicio: el ETag sigue valiendo
        st2 = p2.states[sp.id]
        assert st2.etag == '"v1"' and st2.last_modified == "Fri, 23 Oct 2026 08:00:00 GMT"
        await p2.poll(st2)
        assert seen[2][0] == '"v1"' and st2.ok and len(desk.calls) == 4
        version["v"] = 2   # novedad: ya no es el primer sondeo de esta ejecución
        await p2.poll(st2)
        assert len(desk.calls) == 5 and desk.calls[-1][2] is False and st2.etag == '"v2"'
        assert st2.view()["median_lat_s"] == pytest.approx(120, abs=5)
        p2.close()
    store.close()


async def test_errores_html_y_google_news(tmp_path):
    async def caida(_req):
        return web.Response(status=503)

    async def html(_req):
        return web.Response(text="<html><body>Página no encontrada</body></html>", content_type="text/html")

    async def gnews(_req):
        return web.Response(body=rss(TITLES[:1], google=True), content_type="application/rss+xml")

    app = feed_app({"/caida": caida, "/html": html, "/gn": gnews})
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        specs = [spec("x", str(server.make_url("/caida"))), spec("h", str(server.make_url("/html"))),
                 spec("g", str(server.make_url("/gn")), kind="google_news", interval_s=600)]
        desk = FakeDesk()
        p, _, store = make(tmp_path, specs, session, desk)
        for st in p.states.values():
            await p.sem.acquire()   # _poll libera el hueco al terminar
            await p._poll(st)
        x, h = p.states["x"], p.states["h"]
        assert (x.ok, x.err, x.fails, x.view()["interval_s"]) == (False, "HTTP 503", 1, 240)
        assert (h.ok, h.err) == (False, "Respuesta sin RSS ni Atom") and h.etag is None
        [(item, _, _)] = desk.calls
        assert (item.title, item.via) == (TITLES[0], "日本経済新聞")
        assert len(p.sched) == 3   # los tres vuelven al heap
        p.close()
    store.close()


async def test_espaciado_por_host_en_red_local(tmp_path):
    arrivals = []

    async def handler(_req):
        arrivals.append(time.monotonic())
        return web.Response(body=rss(TITLES[:1]), content_type="application/rss+xml")

    async with TestServer(feed_app({"/a": handler, "/b": handler, "/c": handler})) as server, \
            aiohttp.ClientSession() as session:
        specs = [spec(k, str(server.make_url("/" + k))) for k in "abc"]
        p, _, store = make(tmp_path, specs, session)
        p.start_spread_s = 0
        task = asyncio.create_task(p.run())
        t0 = time.monotonic()
        while len(arrivals) < 3 and time.monotonic() - t0 < 5:
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.05)
        p.close()
        await asyncio.wait_for(task, 5)
    gaps = [b - a for a, b in zip(arrivals, arrivals[1:])]
    assert len(arrivals) == 3 and all(g >= 0.25 for g in gaps), gaps   # 127.0.0.1: 0,3 s entre peticiones
    assert all(st.ok for st in p.states.values())
    store.close()


async def test_con_la_mesa_real(tmp_path):
    async def handler(_req):
        return web.Response(body=rss(["Bitcoin ETF inflows hit record as Nikkei slides"]), content_type="application/rss+xml")

    async with TestServer(feed_app({"/rss": handler})) as server, aiohttp.ClientSession() as session:
        store = Store(tmp_path / "t.db")
        bus = Bus(store)
        sp = spec(url=str(server.make_url("/rss")), lang="en")
        p = FeedPoller(bus, NewsDesk(bus, store), session, store, [sp])
        st = p.states[sp.id]
        st.warmed = True   # como si ya hubiera pasado el primer sondeo
        await p.poll(st)
        assert st.view()["relevant_last_hour"] == 1
        [ev] = [e for e in store.recent_events(10) if e["kind"] == "captured"]
        assert ev["patch"]["srcId"] == sp.id and ev["coins"] == ["BTC"]
    store.close()


# ---------- Registro ----------
def test_url_normalizada():
    assert norm_url("https://www.nikkei.com/rss/") == norm_url("http://nikkei.com/rss")
    a = norm_url("https://news.google.com/rss/search?q=%E6%97%A5%E9%8A%80&hl=ja&gl=JP&ceid=JP:ja")
    b = norm_url("https://news.google.com/rss/search?ceid=JP:ja&hl=ja&q=%E6%97%A5%E9%8A%80&gl=JP")
    c = norm_url("https://news.google.com/rss/search?q=%E6%97%A5%E9%8A%80&hl=en-US&gl=US&ceid=US:en")
    assert a == b and a != c


def test_registro_valida_y_quita_repetidos(tmp_path):
    good = {"id": "jp-nhk-1", "name": "NHK", "url": "https://www3.nhk.or.jp/rss/news/cat0.xml", "lang": "ja",
            "country": "JP", "region": "jp", "kind": "rss", "tier": "T1", "interval_s": None}
    (tmp_path / "jp.json").write_text(json.dumps([
        good,
        {**good, "id": "jp-nhk-2", "url": "http://www3.nhk.or.jp/rss/news/cat0.xml/"},
        {**good, "id": "jp-mal-1", "url": "ftp://x"}, {**good, "id": "jp-mal-2", "url": "https://a.jp/r", "tier": "T9"},
        {**good, "id": "jp-mal-3", "url": "https://b.jp/r", "region": "marte"},
        {**good, "id": "jp-nikkei", "url": "https://assets.wor.jp/rss/rdf/nikkei/news.rdf", "tier": None, "interval_s": 60},
    ]), encoding="utf-8")
    (tmp_path / "us.json").write_text(json.dumps([{**good, "id": "jp-nhk-1", "url": "https://otra.com/rss", "region": "us"}]),
                                      encoding="utf-8")
    (tmp_path / "roto.json").write_text("[{", encoding="utf-8")
    specs = load_registry(tmp_path)
    assert [s.id for s in specs] == ["jp-nhk-1", "jp-nikkei"]
    assert specs[1].tier == "T3" and specs[1].interval_s == 60.0 and specs[0].lang == "ja"
    assert load_registry(tmp_path / "no-existe") == []


# ---------- Importador ----------
VERIF_JP = [
    {"ok": True, "url": "https://www3.nhk.or.jp/rss/news/cat0.xml", "name": "NHKニュース 主要", "lang": "ja",
     "country": "JP", "kind": "rss", "tier": "T1", "newest_age_h": 0.4},
    {"ok": True, "url": "http://www3.nhk.or.jp/rss/news/cat0.xml/", "name": "NHK (repetido)", "lang": "ja",
     "country": "JP", "kind": "rss", "tier": "T1", "newest_age_h": 0.4},
    {"ok": False, "url": "https://www.example.jp/rss", "name": "Caído", "lang": "ja", "kind": "rss", "tier": "T3",
     "newest_age_h": None},
    {"ok": True, "url": "https://news.google.com/rss/search?q=%E6%97%A5%E9%8A%80+when:1d&hl=ja&gl=JP&ceid=JP:ja",
     "name": "Google News: 日銀", "lang": "ja", "country": "JP", "kind": "google_news", "tier": "T2", "newest_age_h": 0.1},
    {"ok": True, "url": "https://news.google.com/rss/search?hl=ja&gl=JP&ceid=JP:ja&q=%E6%97%A5%E9%8A%80%20when%3A1d",
     "name": "Google News: 日銀 (otro orden)", "lang": "ja", "kind": "google_news", "tier": "T2", "newest_age_h": 0.1},
    {"ok": True, "url": "https://assets.wor.jp/rss/rdf/nikkei/markets.rdf", "name": "日経 マーケット", "lang": "ja",
     "country": "JP", "kind": "rss", "tier": "T0", "newest_age_h": 1.2},
]
VERIF_LATAM = [
    {"ok": True, "url": "https://www.infomoney.com.br/feed/", "name": "InfoMoney", "lang": "pt", "country": "BR",
     "kind": "rss", "tier": "T2", "newest_age_h": 0.3},
    {"ok": True, "url": "https://www.ambito.com/rss/pages/economia.xml", "name": "Ámbito · Economía", "lang": "es",
     "country": "AR", "kind": "rss", "tier": "T1", "newest_age_h": 0.8},
]


def write_samples(d):
    d.mkdir()
    (d / "jp-out.json").write_text(json.dumps(VERIF_JP, ensure_ascii=False), encoding="utf-8")
    (d / "latam-br-out.json").write_text(json.dumps(VERIF_LATAM, ensure_ascii=False), encoding="utf-8")
    (d / "xx-out.json").write_text("[]", encoding="utf-8")
    return d


def test_importador_por_region_ids_estables_y_orden(tmp_path):
    src, dest = write_samples(tmp_path / "verif"), tmp_path / "fuentes"
    res = importar(src, dest)
    assert res["regions"] == {"jp": 3, "latam": 2} and res["kinds"] == {"rss": 4, "google_news": 1}
    assert (res["dropped"], res["dupes"], res["skipped"]) == (1, 2, ["xx-out.json"])
    jp = json.loads((dest / "jp.json").read_text(encoding="utf-8"))
    assert [e["tier"] for e in jp] == ["T0", "T1", "T2"]
    assert all(re.fullmatch(r"jp-[a-z0-9-]+-[0-9a-f]{6}", e["id"]) for e in jp)
    assert jp[1]["name"] == "NHKニュース 主要" and jp[1]["id"].startswith("jp-www3-nhk-or-jp-")   # el id sale del host
    assert jp[0]["id"].startswith("jp-assets-wor-jp-") and jp[2]["id"].startswith("jp-gnews-")
    latam = json.loads((dest / "latam.json").read_text(encoding="utf-8"))
    assert [e["name"] for e in latam] == ["Ámbito · Economía", "InfoMoney"] and latam[0]["id"].startswith("latam-ambito-com-")
    ids = [e["id"] for e in jp + latam]
    importar(src, dest)   # otra importación da los mismos ids
    assert ids == [e["id"] for f in ("jp.json", "latam.json") for e in json.loads((dest / f).read_text(encoding="utf-8"))]
    specs = load_registry(dest)
    assert len(specs) == 5 and {s.region for s in specs} == {"jp", "latam"}


def test_importador_cli_muestra_recuento(tmp_path, capsys):
    src = write_samples(tmp_path / "verif")
    assert importar_main([str(src), "--destino", str(tmp_path / "fuentes")]) == 0
    out = capsys.readouterr().out
    assert "jp" in out and "Japón" in out and "Latinoamérica" in out and "google_news 1" in out and "Total: 5" in out
    assert importar_main([str(tmp_path / "nada")]) == 2


# ---------- Resumen y API ----------
def test_resumen_por_region_y_api(tmp_path):
    specs = [spec("jp-a", "https://a.jp/rss"), spec("jp-b", "https://b.jp/rss", tier="T2"),
             spec("us-a", "https://a.com/rss", region="us", lang="en")]
    p, bus, store = make(tmp_path, specs)
    now = now_ms()
    a, b = p.states["jp-a"], p.states["jp-b"]
    a.on_success(2)
    a.record("captured", now - 30_000, now, warm=False)
    a.record("filtered", now - 50_000, now, warm=False)
    a.record(None, now - 10_000, now, warm=False)
    a.record("filtered", now - 9_000_000, now, warm=True)   # histórico: cuenta pero no entra en la latencia
    a.hits.appendleft((now - 3_700_000, True))               # de hace más de una hora: no cuenta
    b.on_error("Tiempo agotado (20 s)")
    s = p.summary()
    assert s["totals"] == {"feeds": 3, "ok": 1, "failing": 1, "waiting": 1, "items_last_hour": 3, "relevant_last_hour": 1,
                           "sin_red": False}
    jp, us = s["regions"]
    assert (jp["region"], jp["name"], jp["market"], jp["feeds"], jp["ok"], jp["failing"], jp["median_lat_s"]) == \
        ("jp", "Japón", "tokio", 2, 1, 1, 40)
    assert (us["region"], us["market"], us["waiting"], us["median_lat_s"]) == ("us", "nuevayork", 1, None)
    assert p.publish_summary() and not p.publish_summary()   # solo si cambia
    msg = bus.state["sources_summary"]
    assert msg["type"] == "sources_summary" and msg["ts"] > 0 and msg["regions"][0]["region"] == "jp"
    rows = p.api_list("jp")
    assert [r["id"] for r in rows] == ["jp-a", "jp-b"] and len(p.api_list()) == 3 and p.api_list("kr") == []
    assert set(rows[0]) == {"id", "name", "url", "region", "tier", "kind", "lang", "ok", "err", "last_ok", "items_total",
                            "items_last_hour", "relevant_last_hour", "interval_s", "median_lat_s", "skew_h", "estado", "peso"}
    assert (rows[0]["estado"], rows[0]["peso"]) == ("activa", 1.0)
    assert (rows[0]["items_total"], rows[0]["items_last_hour"], rows[1]["err"], rows[1]["interval_s"]) == \
        (3, 3, "Tiempo agotado (20 s)", 360)
    store.close()


# ---------- Misma entrada por varios feeds ----------
def gnews_rss(entries) -> bytes:
    """Muestra recortada de Google News: «Titular - Medio», guid propio y <source url> con la web del medio."""
    now = datetime.now(timezone.utc)
    items = "".join(
        f"<item><title>{t} - {medio}</title><link>https://news.google.com/rss/articles/{g}?oc=5</link>"
        f'<guid isPermaLink="false">{g}</guid><pubDate>{format_datetime(now - timedelta(minutes=3))}</pubDate>'
        f'<source url="{web_}">{medio}</source></item>'
        for t, g, medio, web_ in entries)
    return ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Google News</title>'
            f"{items}</channel></rss>").encode()


def test_dominio_del_medio_y_clave_de_entrada():
    assert [site(h) for h in ("www3.nhk.or.jp", "asia.nikkei.com", "news.bbc.co.uk", "finance.sina.com.cn",
                              "www.scmp.com", "127.0.0.1")] == \
        ["nhk.or.jp", "nikkei.com", "bbc.co.uk", "sina.com.cn", "scmp.com", "127.0.0.1"]
    a = Item("x", "1", "t", "https://www.nikkei.com/article/1/?utm_source=rss&utm_medium=feed")
    b = Item("y", "2", "t", "http://nikkei.com/article/1")
    assert entry_key(a, False, "x") == entry_key(b, False, "y")
    assert entry_key(Item("x", "CBMi1", "t", "https://news.google.com/rss/articles/CBMi1?oc=5"), True, "") == "gn|CBMi1"
    assert entry_key(Item("x", "abc", "t", ""), False, "a.jp") == "a.jp|abc"   # sin enlace: host del feed y guid


def test_entregadas_caducan_y_tienen_tope():
    d = Delivered(ttl_ms=1000, cap=3)
    assert d.owner("x", "a", 0) == "a" and d.owner("x", "b", 500) == "a"
    assert d.owner("x", "b", 1400) == "a"   # se renovó al verla a los 500 ms
    assert d.owner("x", "b", 2500) == "b"   # caducó
    for k in "pqrs":
        d.owner(k, "a", 2600)
    assert len(d) == 3


async def test_misma_entrada_en_varios_feeds_pasa_una_vez(tmp_path):
    gn = gnews_rss([("BOJ holds rates steady", "CBMi1", "Nikkei Asia", "https://asia.nikkei.com"),
                    ("Yen slides past 152", "CBMi2", "Reuters", "https://www.reuters.com")])
    nhk = rss(TITLES[:2])
    bodies = {"gn-a": gn, "gn-b": gn, "gn-c": gn, "nhk-0": nhk,
              "nhk-5": nhk.replace(b"<guid>k", b"<guid>c5-k")}   # otra categoría: mismo enlace, otro guid
    specs = [spec("gn-a", "https://news.google.com/rss/search?q=boj&ceid=US:en", kind="google_news"),
             spec("gn-b", "https://news.google.com/rss/search?q=yen&ceid=US:en", kind="google_news"),
             spec("gn-c", "https://news.google.com/rss/search?q=nikkei&ceid=US:en", kind="google_news"),
             spec("nhk-0", "https://www3.nhk.or.jp/rss/news/cat0.xml"), spec("nhk-5", "https://www3.nhk.or.jp/rss/news/cat5.xml")]
    desk = FakeDesk()
    p, _, store = make(tmp_path, specs, desk=desk)

    async def fake_get(st):
        return 200, bodies[st.spec.id], None, None

    p._get = fake_get
    for fid in bodies:
        await p.poll(p.states[fid])
    got = sorted((it.source, it.uid, it.outlet) for it, _, _ in desk.calls)
    assert got == [("gn-a", "CBMi1", "nikkei.com"), ("gn-a", "CBMi2", "reuters.com"),
                   ("nhk-0", "k0", "nikkei.com"), ("nhk-0", "k1", "nikkei.com")]
    assert all(p.states[f].ok and p.states[f].items_total == 0 for f in ("gn-b", "gn-c", "nhk-5"))
    # Las repetidas quedan como vistas para la mesa: tras un reinicio no salen como nuevas por el otro feed.
    assert store.is_seen("gn-b|CBMi1") and store.is_seen("gn-c|CBMi2") and store.is_seen("nhk-5|c5-k0")
    bodies["gn-b"] = gnews_rss([("BOJ holds rates steady", "CBMi1", "Nikkei Asia", "https://asia.nikkei.com"),
                                ("Nikkei hits record", "CBMi3", "Bloomberg", "https://www.bloomberg.com")])
    await p.poll(p.states["gn-b"])
    assert [(it.source, it.uid, it.outlet) for it, _, _ in desk.calls[4:]] == [("gn-b", "CBMi3", "bloomberg.com")]
    store.close()


def test_medio_de_la_entrada():
    gn = FeedState(spec("g", "https://news.google.com/rss/search?q=boj", kind="google_news"))
    rss_ = FeedState(spec("r", "https://www.example.co.jp/rss.xml"))
    out = FeedPoller._outlet
    assert out(gn, Item("g", "1", "t", via="Nikkei Asia"), {"Nikkei Asia": "nikkei.com"}) == "nikkei.com"
    assert out(gn, Item("g", "1", "t", via="Kyodo  News"), {}) == "kyodo news"   # sin <source>: el nombre
    assert out(rss_, Item("r", "1", "t", "https://feeds.feedburner.com/~r/x/1"), {}) == "example.co.jp"   # redirector
    assert out(rss_, Item("r", "1", "t", "https://www3.nhk.or.jp/news/1.html"), {}) == "nhk.or.jp"


class CountingDesk(NewsDesk):
    def __init__(self, *a):
        super().__init__(*a)
        self.n = 0

    def handle(self, item, src, warm):
        self.n += 1
        return super().handle(item, src, warm)


async def test_varias_consultas_de_google_news_con_la_mesa_real(tmp_path):
    body = gnews_rss([("Bitcoin ETF inflows hit record as Nikkei slides", "CBMiAAA", "Nikkei Asia", "https://asia.nikkei.com")])

    async def handler(_req):
        return web.Response(body=body, content_type="application/rss+xml")

    app = feed_app({"/a": handler, "/b": handler, "/c": handler})
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        store = Store(tmp_path / "t.db")
        bus = Bus(store)
        specs = [spec(f"jp-gn-{k}", str(server.make_url(f"/{k}")), kind="google_news", lang="en") for k in "abc"]
        desk = CountingDesk(bus, store)
        p = FeedPoller(bus, desk, session, store, specs)
        for s in specs:
            await p.poll(p.states[s.id])
        kinds = [e["kind"] for e in store.recent_events(50) if e["type"] == "event"]
        assert kinds == ["captured"] and desk.n == 1   # antes: 1 captured y 2 corroborated del mismo medio
        [ev] = [e for e in store.recent_events(50) if e.get("kind") == "captured"]
        assert ev["patch"]["via"] == "Nikkei Asia" and ev["coins"] == ["BTC"]
        p.close()
        # Reinicio: la mesa ya no recuerda la noticia, pero el feed «b», que llega primero, no la vuelve a dar.
        p2 = FeedPoller(bus, NewsDesk(bus, store), session, store, specs)
        for fid in ("jp-gn-b", "jp-gn-a", "jp-gn-c"):
            await p2.poll(p2.states[fid])
        assert [e["kind"] for e in store.recent_events(50) if e["type"] == "event"] == ["captured"]
        p2.close()
    store.close()


async def test_titular_japones_con_la_mesa_real(tmp_path):
    async def handler(_req):
        return web.Response(body=rss(["ビットコイン急伸、米国でETF資金流入が拡大"]), content_type="application/rss+xml")

    async with TestServer(feed_app({"/rss": handler})) as server, aiohttp.ClientSession() as session:
        store = Store(tmp_path / "t.db")
        bus = Bus(store)
        sp = spec(url=str(server.make_url("/rss")), lang="ja")
        p = FeedPoller(bus, NewsDesk(bus, store), session, store, [sp])
        st = p.states[sp.id]
        st.warmed = True
        await p.poll(st)
        assert st.view()["relevant_last_hour"] == 1
        [ev] = [e for e in store.recent_events(10) if e.get("kind") == "captured"]
        assert ev["coins"] == ["BTC"] and ev["patch"]["region"] == "jp" and ev["patch"]["lang"] == "ja"
    store.close()


# ---------- Fechas adelantadas (hora local marcada como UTC) ----------
def test_fechas_adelantadas_se_corrigen_por_feed():
    now, h, m = now_ms(), 3_600_000, 60_000
    st = FeedState(spec())
    items = [Item("x", str(i), "t", published=now + 9 * h - k * m) for i, k in enumerate((2, 5, 30, 600))]
    st.fix_times(items, now)
    assert st.skew_h == 9 and st.view()["skew_h"] == 9
    assert [round((now - it.published) / m) for it in items] == [2, 5, 30, 600]
    # Una sola fecha en el futuro (nota programada) no cambia el feed: esa entrada queda sin hora.
    ok = FeedState(spec())
    items = [Item("x", "a", "t", published=now + 3 * h), Item("x", "b", "t", published=now - 5 * m),
             Item("x", "c", "t", published=now + 5 * m)]
    ok.fix_times(items, now)
    assert ok.skew_h == 0 and [it.published for it in items] == [None, now - 5 * m, now + 5 * m]
    # Más de 14 h no es una zona horaria: no se corrige, se quita la hora.
    far = FeedState(spec())
    items = [Item("x", str(i), "t", published=now + 20 * h + i * m) for i in range(3)]
    far.fix_times(items, now)
    assert far.skew_h == 0 and all(it.published is None for it in items)


async def test_feed_con_hora_local_como_gmt(tmp_path):
    now = datetime.now(timezone.utc)
    items = "".join(f"<item><title>北京时间 {i}</title><link>https://www.example.cn/{i}</link><guid>g{i}</guid>"
                    f"<pubDate>{format_datetime(now + timedelta(hours=8, minutes=-k))}</pubDate></item>"
                    for i, k in enumerate((1, 4, 20)))
    body = f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>x</title>{items}</channel></rss>'
    desk = FakeDesk()
    p, _, store = make(tmp_path, [spec("cn-x", "https://www.example.cn/rss", region="cn", lang="zh")], desk=desk)

    async def fake_get(st):
        return 200, body.encode(), None, None

    p._get = fake_get
    st = p.states["cn-x"]
    await p.poll(st)
    ages = sorted(round((now_ms() - it.published) / 60_000) for it, _, _ in desk.calls)
    assert st.skew_h == 8 and ages == [1, 4, 20]
    store.close()


# ---------- Esperas, hosts y errores ----------
def test_espera_por_errores_no_desborda():
    st = FeedState(spec(interval_s=60))
    st.restore(None, None, None, None, 5000)   # feed muerto durante semanas
    assert st.next_wait() == 1800 and st.view()["interval_s"] == 1800
    st.fails = 1100
    assert st.next_wait() == 1800


async def test_feed_vuelve_al_heap_aunque_falle_la_espera(tmp_path):
    p, _, store = make(tmp_path, [spec()])
    st = p.states[spec().id]

    async def fake_get(_st):
        return 304, b"", None, None

    p._get = fake_get
    st.next_wait = lambda: 1 / 0
    await p.sem.acquire()
    await p._poll(st)
    assert len(p.sched) == 1
    store.close()


def test_host_tope_a_la_vez_y_pausa():
    g = HostGate()
    for _ in range(3):
        g.start("a.jp", 0.0)
    assert g.busy("a.jp") and not g.busy("b.jp")
    g.done("a.jp")
    assert not g.busy("a.jp")
    e429 = aiohttp.ClientResponseError(None, (), status=429, headers={"Retry-After": "120"})
    assert g.failed("a.jp", 100.0, e429) == 120 and g.delay("a.jp", 100.0) == 120
    g.mark("a.jp", 100.0)
    assert g.delay("a.jp", 100.0) == 120   # una petición nueva no acorta la pausa
    e503 = aiohttp.ClientResponseError(None, (), status=503)
    assert [g.failed("news.google.com", 0.0, e503) for _ in range(3)] == [30, 60, 120]   # crece ×2
    g.ok("news.google.com")
    assert g.failed("news.google.com", 0.0, e503) == 30
    assert [g.failed("t.jp", 0.0, asyncio.TimeoutError()) for _ in range(6)] == [0, 0, 0, 0, 30, 60]
    assert g.failed("t.jp", 0.0, aiohttp.ClientResponseError(None, (), status=404)) == 0 and "t.jp" not in g.fails
    when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=300), usegmt=True)
    assert g.failed("d.jp", 0.0, aiohttp.ClientResponseError(None, (), status=429, headers={"Retry-After": when})) == \
        pytest.approx(300, abs=2)


async def test_tope_de_peticiones_a_la_vez_por_host(tmp_path):
    specs = [spec(f"s{i}", f"https://lento.example/rss{i}") for i in range(6)] + [spec("f", "https://rapido.example/rss")]
    p, _, store = make(tmp_path, specs)
    t, inside, peak, polled = [0.0], [0], [0], {}
    release = asyncio.Event()

    async def fake_get(st):
        polled[st.spec.id] = t[0]
        if st.host == "lento.example":   # host colgado: no responde hasta que se suelta
            inside[0] += 1
            peak[0] = max(peak[0], inside[0])
            await release.wait()
            inside[0] -= 1
        return 304, b"", None, None

    async def fake_sleep(d):
        for _ in range(3):
            await asyncio.sleep(0)
        t[0] += d
        if t[0] > 30:
            release.set()
        if t[0] > 60:
            p.close()

    p._get, p.clock, p.sleep, p.jitter, p.start_spread_s = fake_get, lambda: t[0], fake_sleep, 0, 0
    await asyncio.wait_for(p.run(), 10)
    assert peak[0] == 3 and polled["f"] < 1   # el otro host no espera al colgado
    assert set(polled) == {s.id for s in specs}
    store.close()


async def test_429_frena_el_host_y_respeta_retry_after(tmp_path):
    async def handler(_req):
        return web.Response(status=429, headers={"Retry-After": "120"})

    async with TestServer(feed_app({"/rss": handler})) as server, aiohttp.ClientSession() as session:
        sp = spec(url=str(server.make_url("/rss")))
        p, _, store = make(tmp_path, [sp], session)
        st = p.states[sp.id]
        await p.sem.acquire()
        await p._poll(st)
        assert st.err == "HTTP 429" and p.gate.delay(st.host, p.clock()) == pytest.approx(120, abs=1)
    store.close()


async def test_flush_durante_el_sondeo_no_pierde_el_etag(tmp_path):
    p, _, store = make(tmp_path, [spec()])
    st = p.states[spec().id]
    go = asyncio.Event()

    async def fake_get(_st):
        await go.wait()
        return 200, rss(), '"etag-1"', None

    p._get = fake_get
    task = asyncio.create_task(p.poll(st))
    await asyncio.sleep(0)
    p.flush()   # el resumen guarda mientras la petición está en vuelo
    go.set()
    await task
    p.close()
    assert store.db.execute("SELECT etag FROM feed_state WHERE id=?", (st.spec.id,)).fetchone() == ('"etag-1"',)
    store.close()


async def test_respuesta_troceada_demasiado_grande(tmp_path, monkeypatch):
    monkeypatch.setattr(feeds, "MAX_BYTES", 2000)

    async def big(req):
        resp = web.StreamResponse()
        resp.enable_chunked_encoding()   # sin Content-Length
        await resp.prepare(req)
        for _ in range(10):
            await resp.write(b"<rss>" + b"x" * 1000)
        await resp.write_eof()
        return resp

    async with TestServer(feed_app({"/rss": big})) as server, aiohttp.ClientSession() as session:
        sp = spec(url=str(server.make_url("/rss")))
        p, _, store = make(tmp_path, [sp], session)
        st = p.states[sp.id]
        await p.sem.acquire()
        await p._poll(st)
        assert (st.ok, st.err) == (False, "Respuesta demasiado grande")
    store.close()


def test_errores_de_red_en_espanol():
    key = SimpleNamespace(host="www3.nhk.or.jp", port=443, ssl=True)
    assert short_error(aiohttp.ClientConnectorError(key, OSError(11001, "getaddrinfo failed"))) == "Sin conexión con el servidor"
    assert short_error(aiohttp.ClientConnectorCertificateError(key, Exception("x"))) == "Error de certificado o SSL"
    assert short_error(aiohttp.TooManyRedirects(None, ())) == "Demasiadas redirecciones"
    assert short_error(aiohttp.ServerDisconnectedError()) == "El servidor cortó la conexión"
    assert short_error(aiohttp.ClientPayloadError("x")) == "Respuesta cortada"
    assert short_error(asyncio.TimeoutError()) == "Tiempo agotado (20 s)"
    assert short_error(aiohttp.ClientOSError(104, "Connection reset by peer")) == "Error de conexión"
    assert short_error(RuntimeError("boom")) == "Error inesperado (RuntimeError: boom)"


async def test_poll_cede_el_bucle_con_muchas_entradas(tmp_path):
    ticks = [0]

    async def ticker():
        while True:
            ticks[0] += 1
            await asyncio.sleep(0)

    class TickDesk(FakeDesk):
        def handle(self, item, src, warm):
            self.calls.append(ticks[0])
            return "captured"

    desk = TickDesk()
    p, _, store = make(tmp_path, [spec()], desk=desk)

    async def fake_get(_st):
        return 200, rss([f"Titular {i}" for i in range(60)]), None, None

    p._get = fake_get
    task = asyncio.create_task(ticker())
    await p.poll(p.states[spec().id])
    task.cancel()
    assert len(desk.calls) == 60 and len(set(desk.calls)) >= 3   # cede al menos cada 20 entradas
    store.close()


# ---------- Google News, reparto inicial y BOM ----------
def test_google_news_por_host_y_capacidad(tmp_path, caplog):
    sp = spec("g", "https://news.google.com/rss/search?q=boj&ceid=JP:ja", kind="rss")   # mal etiquetado
    assert FeedState(sp).base == 600 and FeedState(sp).gnews
    many = [spec(f"g{i}", f"https://news.google.com/rss/search?q=q{i}&ceid=JP:ja", kind="google_news") for i in range(600)]
    with caplog.at_level("WARNING", logger="feeds"):
        p, _, store = make(tmp_path, many)
    assert "600 consultas" in caplog.text
    assert p.states["g0"].view()["interval_s"] == 720   # 600 × 1,2 s: el intervalo real, no 600
    store.close()


def test_reparto_inicial_crece_con_el_registro(tmp_path):
    p, _, store = make(tmp_path, [spec(f"f{i}", f"https://h{i}.example/rss") for i in range(2400)])
    assert p.start_spread_s == pytest.approx(2400 / feeds.config.FEEDS_CONCURRENCY * 2)
    store.close()
    p2, _, store2 = make(tmp_path / "b", [spec()])
    assert p2.start_spread_s == 60
    store2.close()


def test_registro_e_importador_con_bom(tmp_path):
    good = {"id": "jp-nhk-1", "name": "NHK", "url": "https://www3.nhk.or.jp/rss/news/cat0.xml", "region": "jp"}
    (tmp_path / "jp.json").write_text(json.dumps([good, {**good, "id": "jp-x", "url": "https://a.jp:abc/rss"}]),
                                      encoding="utf-8-sig")
    assert [s.id for s in load_registry(tmp_path)] == ["jp-nhk-1"]   # el puerto mal escrito se descarta, no rompe
    src = tmp_path / "verif"
    src.mkdir()
    (src / "jp-out.json").write_text(json.dumps(VERIF_JP, ensure_ascii=False), encoding="utf-8-sig")
    assert importar(src, tmp_path / "fuentes")["regions"] == {"jp": 3}


def test_importador_conserva_ids_si_cambia_el_nombre_o_la_region(tmp_path):
    src, dest = write_samples(tmp_path / "verif"), tmp_path / "fuentes"
    importar(src, dest)
    ids = {e["url"]: e["id"] for e in json.loads((dest / "jp.json").read_text(encoding="utf-8"))}
    renamed = [{**d, "name": d["name"] + " (retocado)"} for d in VERIF_JP]
    (src / "jp-out.json").write_text(json.dumps(renamed, ensure_ascii=False), encoding="utf-8")
    importar(src, dest)
    assert {e["url"]: e["id"] for e in json.loads((dest / "jp.json").read_text(encoding="utf-8"))} == ids
    importar(src, tmp_path / "nuevo")   # sin registro previo, el id tampoco depende del nombre
    assert {e["url"]: e["id"] for e in json.loads((tmp_path / "nuevo" / "jp.json").read_text(encoding="utf-8"))} == ids
    # El NHK pasa a otro archivo de región: conserva el id que ya tenía (y con él su feed_state y sus vistos).
    nhk = renamed[0]
    (src / "jp-out.json").write_text(json.dumps(renamed[1:], ensure_ascii=False), encoding="utf-8")
    (src / "hk-out.json").write_text(json.dumps([nhk], ensure_ascii=False), encoding="utf-8")
    importar(src, dest)
    [hk] = json.loads((dest / "hk.json").read_text(encoding="utf-8"))
    assert hk["region"] == "hk" and hk["id"] == ids[nhk["url"]]


def test_importador_respeta_ids_antiguos(tmp_path):
    src, dest = write_samples(tmp_path / "verif"), tmp_path / "fuentes"
    dest.mkdir()
    old = [{"id": "jp-nhk-abc123", "name": "NHK", "url": "https://www3.nhk.or.jp/rss/news/cat0.xml", "region": "jp"}]
    (dest / "jp.json").write_text(json.dumps(old), encoding="utf-8-sig")
    importar(src, dest)
    jp = json.loads((dest / "jp.json").read_text(encoding="utf-8"))
    assert [e["id"] for e in jp if "nhk" in e["url"]] == ["jp-nhk-abc123"]


# --- feedparser fuera del proceso (convoy del GIL) ---

async def test_feedparser_se_lee_en_otro_proceso(tmp_path):
    p, _, store = make(tmp_path, [spec()])
    p.parse_pool = ProcessPoolExecutor(max_workers=1)
    try:
        items = await asyncio.wait_for(p._parse(rss(TITLES[:2]), "x"), 60)
    finally:
        p.close()
    assert [i.title for i in items] == TITLES[:2] and all(isinstance(i, Item) for i in items)
    assert p.parse_pool is None   # close() cierra los procesos
    store.close()


class _FakePool:
    def __init__(self, broken=False):
        self.broken, self.down, self.used = broken, False, 0

    def submit(self, fn, *args):
        if self.broken:
            raise BrokenProcessPool("caído")
        self.used += 1
        f = Future()
        f.set_result(fn(*args))
        return f

    def shutdown(self, **_kw):
        self.down = True


async def test_si_el_proceso_cae_se_lee_en_un_hilo_y_se_recrea(tmp_path, monkeypatch):
    p, _, store = make(tmp_path, [spec()])
    fresh = _FakePool()
    monkeypatch.setattr(feeds, "ProcessPoolExecutor", lambda max_workers: fresh)
    p.parse_pool = broken = _FakePool(broken=True)
    items = await p._parse(rss(TITLES[:1]), "x")
    assert [i.title for i in items] == TITLES[:1] and broken.down and p.parse_pool is fresh
    assert len(await p._parse(rss(TITLES[:3]), "x")) == 3 and fresh.used == 1
    p.close()
    assert fresh.down
    store.close()


# ---------- Ciclo de vida (fuentes vivas) ----------
def test_silenciada_se_sondea_menos_y_peso():
    st = FeedState(spec(tier="T1"))
    assert (st.estado, st.peso, st.next_wait()) == ("activa", 1.0, 120)
    st.estado = "prueba"
    assert (st.peso, st.next_wait()) == (0.5, 120)   # prueba: sondeo normal, peso bajo
    st.estado = "silenciada"
    assert (st.peso, st.next_wait()) == (0.0, 1800)  # ×4 y como poco cada 30 min
    st.on_error("HTTP 503")
    st.on_error("HTTP 503")
    assert st.next_wait() == 480 * 4                  # la espera por errores también se alarga
    assert st.view()["estado"] == "silenciada" and st.view()["peso"] == 0.0


async def test_retirada_no_se_sondea_y_vuelve_al_restaurar(tmp_path):
    specs = [spec("a", "https://a.example/rss", tier="T0"), spec("r", "https://r.example/rss", tier="T0"),
             spec("s", "https://s.example/rss", tier="T0")]
    store = Store(tmp_path / "t.db")
    p = FeedPoller(Bus(store), FakeDesk(), None, store, specs, estados={"r": "retirada", "s": "silenciada", "x": "activa"})
    p.parse_workers = 0
    t = [0.0]
    polls: dict[str, list[float]] = {}

    async def fake_get(st):
        polls.setdefault(st.spec.id, []).append(t[0])
        return 304, b"", None, None

    async def fake_sleep(d):
        for _ in range(3):
            await asyncio.sleep(0)
        t[0] += d
        if 400 <= t[0] < 400 + d:   # a los 400 s se restaura «r» y se retira «a»
            assert p.set_estado("r", "activa") and p.set_estado("a", "retirada")
        if t[0] > 1000:
            p.close()

    p._get, p.clock, p.sleep, p.jitter, p.start_spread_s = fake_get, lambda: t[0], fake_sleep, 0, 0
    assert not p.set_estado("no-existe", "activa") and not p.set_estado("a", "inventado")
    await asyncio.wait_for(p.run(), 10)
    assert polls["a"] == pytest.approx([0, 90, 180, 315])              # retirada a los 400 s: no vuelve
    assert polls["r"][0] == pytest.approx(400, abs=31) and len(polls["r"]) >= 4
    assert polls["s"] == pytest.approx([0])                             # silenciada: la siguiente, a los 1800 s
    assert "a" in p.parked and "r" not in p.parked
    tot = p.summary()["totals"]
    assert tot["feeds"] == 2   # la retirada no cuenta en el resumen
    store.close()


async def test_entradas_marcadas_por_estado_del_feed_y_del_medio(tmp_path):
    gn = gnews_rss([("Bitcoin ETF inflows hit record", "CBMi1", "Nikkei Asia", "https://asia.nikkei.com"),
                    ("Yen slides past 152", "CBMi2", "Rumores Cripto", "https://www.rumores.example")])
    desk = FakeDesk(("captured", "filtered"))
    p, _, store = make(tmp_path, [spec("gn", "https://news.google.com/rss/search?q=boj&ceid=US:en", kind="google_news"),
                                  spec("nhk", "https://www3.nhk.or.jp/rss/news/cat0.xml")], desk=desk)
    seen = []
    p.on_item = lambda st, it, res: seen.append((st.spec.id, it.outlet, it.estado, it.peso, res))
    p.set_outlet_estado("rumores.example", "silenciada")
    p.set_estado("nhk", "prueba")

    async def fake_get(st):
        return 200, gn if st.gnews else rss(TITLES[:1]), None, None

    p._get = fake_get
    await p.poll(p.states["gn"])
    await p.poll(p.states["nhk"])
    assert seen == [("gn", "nikkei.com", "activa", 1.0, "captured"), ("gn", "rumores.example", "silenciada", 0.0, "filtered"),
                    ("nhk", "nikkei.com", "prueba", 0.5, "captured")]
    p.set_outlet_estado("rumores.example", "activa")   # restaurado: sale de la lista
    assert p.outlet_estados == {}


def test_entregadas_guardan_el_mejor_estado():
    d = Delivered(ttl_ms=10_000)
    assert d.deliver("x", "a", 0, "silenciada") == ("a", None)          # nueva
    assert d.deliver("x", "b", 10, "activa") == ("a", "silenciada")     # la entregó «a», con peor estado
    assert d.deliver("x", "c", 20, "prueba") == ("a", "activa")         # queda apuntado el mejor
    assert d.owner("x", "a", 30) == "a"


async def test_repetida_de_un_feed_silenciado_sube_de_estado_por_uno_activo(tmp_path):
    """Dos consultas de Google News que se solapan: la silenciada la entrega antes. La copia de la activa pasa a la mesa
    como mejora de estado (it.mejora) en vez de perderse; las siguientes vueltas ya no la repiten."""
    gn = gnews_rss([("BOJ holds rates steady", "CBMi1", "Nikkei Asia", "https://asia.nikkei.com")])
    desk = FakeDesk()
    p, _, store = make(tmp_path, [spec("jp-gn1", "https://news.google.com/rss/search?q=boj&ceid=JP:en", kind="google_news"),
                                  spec("jp-gn2", "https://news.google.com/rss/search?q=yen&ceid=JP:en", kind="google_news")],
                       desk=desk)
    p.set_estado("jp-gn1", "silenciada")

    async def fake_get(_st):
        return 200, gn, None, None

    p._get = fake_get
    await p.poll(p.states["jp-gn1"])
    await p.poll(p.states["jp-gn2"])
    got = [(it.source, it.estado, it.peso, it.mejora) for it, _, _ in desk.calls]
    assert got == [("jp-gn1", "silenciada", 0.0, None), ("jp-gn2", "activa", 1.0, "silenciada")]
    for fid in ("jp-gn2", "jp-gn1"):   # ya subió: ni la activa ni la silenciada la vuelven a dar
        p.states[fid].digest = None
        await p.poll(p.states[fid])
    assert len(desk.calls) == 3 and desk.calls[2][0].source == "jp-gn1"   # su propio feed: la mesa la ve como vista
    # Al revés (la activa primero) no hay mejora: la silenciada no pasa.
    desk2 = FakeDesk()
    p2 = FeedPoller(Bus(store), desk2, None, store, [spec("a1", "https://news.google.com/rss/search?q=a&ceid=JP:en",
                                                          kind="google_news"),
                                                     spec("s1", "https://news.google.com/rss/search?q=s&ceid=JP:en",
                                                          kind="google_news")])
    p2.parse_workers, p2._get = 0, fake_get
    p2.set_estado("s1", "silenciada")
    await p2.poll(p2.states["a1"])
    await p2.poll(p2.states["s1"])
    assert [(it.source, it.mejora) for it, _, _ in desk2.calls] == [("a1", None)]
    store.close()


async def test_mejora_de_estado_con_la_mesa_real_no_crea_otra_historia(tmp_path):
    gn = gnews_rss([("Bitcoin ETF inflows hit record as Nikkei slides", "CBMiZZ", "Nikkei Asia", "https://asia.nikkei.com")])
    store = Store(tmp_path / "t.db")
    bus = Bus(store)
    desk = CountingDesk(bus, store)
    p = FeedPoller(bus, desk, None, store, [spec("jp-gn1", "https://news.google.com/rss/search?q=a&ceid=JP:en",
                                                 kind="google_news", lang="en"),
                                            spec("jp-gn2", "https://news.google.com/rss/search?q=b&ceid=JP:en",
                                                 kind="google_news", lang="en")])
    p.parse_workers = 0
    p.set_estado("jp-gn1", "silenciada")

    async def fake_get(_st):
        return 200, gn, None, None

    p._get = fake_get
    for fid in ("jp-gn1", "jp-gn2"):
        p.states[fid].warmed = True
        await p.poll(p.states[fid])
    assert desk.n == 2   # la mejora llega a la mesa...
    # ...sin duplicar la historia: la mesa la sube de estado (srcstate)
    evs = [e for e in store.recent_events(50) if e["type"] == "event"]
    assert [e["kind"] for e in evs] == ["captured", "srcstate"] and evs[0]["story"] == evs[1]["story"]
    assert evs[0]["patch"]["srcState"] == "silenciada" and evs[1]["patch"] == {"srcState": "activa", "srcWeight": 1.0}
    store.close()


async def test_gancho_roto_no_para_el_sondeo(tmp_path, caplog):
    desk = FakeDesk()
    p, _, store = make(tmp_path, [spec()], desk=desk)

    async def fake_get(_st):
        return 200, rss(TITLES[:3]), None, None

    def roto(*_a):
        raise RuntimeError("boom")

    p._get, p.on_item = fake_get, roto
    with caplog.at_level("ERROR", logger="feeds"):
        await p.poll(p.states[spec().id])
    assert len(desk.calls) == 3 and p.states[spec().id].ok and p._hook_errors == 3
    assert caplog.text.count("El gancho de fuentes vivas falló") == 1   # no inunda el registro
    store.close()


async def test_anadir_feed_en_marcha(tmp_path):
    p, _, store = make(tmp_path, [spec("g1", "https://news.google.com/rss/search?q=a&ceid=JP:ja", kind="google_news")])
    nuevo = spec("jp-nuevo", "https://www.nuevo.example/feed", tier="T3")
    assert p.add_feed(nuevo)                                                      # antes de arrancar: lo pone run()
    assert not p.add_feed(nuevo)                                                  # mismo id
    assert not p.add_feed(spec("otro", "http://nuevo.example/feed/", tier="T3"))  # misma URL
    assert p.states["jp-nuevo"].estado == "prueba" and len(p.sched) == 0
    g2 = spec("g2", "https://news.google.com/rss/search?q=b&ceid=JP:ja", kind="google_news")
    p._started = True
    assert p.add_feed(g2, "activa") and len(p.sched) == 1                         # en marcha: entra al heap
    assert p.states["g1"].floor_s == pytest.approx(2.4)                           # 2 consultas × 1,2 s
    assert p.set_estado("g2", "retirada") and p.states["g1"].floor_s == pytest.approx(1.2)
    assert p.add_feed(spec("ret", "https://ret.example/rss"), "retirada") and "ret" in p.parked
    store.close()


def test_importador_anade_descubiertas_y_las_conserva(tmp_path):
    from bot.importar_fuentes import anadir
    src, dest = write_samples(tmp_path / "verif"), tmp_path / "fuentes"
    importar(src, dest)
    nuevo = {"id": "jp-nuevo-example-abc123", "name": "Nuevo Diario", "url": "https://nuevo.example/rss.xml", "lang": "ja",
             "country": None, "region": "jp", "kind": "rss", "tier": "T3", "interval_s": None, "origen": "descubierta",
             "alta": "2026-10-27"}
    assert anadir(nuevo, dest) and not anadir({**nuevo, "id": "otro"}, dest)   # la misma URL no entra dos veces
    assert not list(dest.glob("*.tmp"))                                         # escritura atómica: sin restos
    with pytest.raises(ValueError):
        anadir({**nuevo, "region": "marte"}, dest)
    assert "jp-nuevo-example-abc123" in [s.id for s in load_registry(dest)]
    res = importar(src, dest)   # otra importación del verificador: el descubierto sigue en su región
    jp = json.loads((dest / "jp.json").read_text(encoding="utf-8"))
    assert [e["id"] for e in jp if e.get("origen") == "descubierta"] == ["jp-nuevo-example-abc123"]
    assert res["regions"] == {"jp": 3, "latam": 2} and res["kept"] == 1 and res["total"] == 5
    assert anadir({**nuevo, "id": "global-x", "url": "https://x.example/rss", "region": "global"}, dest)
    assert json.loads((dest / "global.json").read_text(encoding="utf-8"))[0]["id"] == "global-x"



async def test_sin_red_no_suma_fallos_y_al_volver_reprograma_todo(tmp_path):
    """Hallazgo n.º 44: en un corte de red del PC, los 5.714 feeds acumulaban fallos y pausas y tardaban hasta 30 min en
    volver. Ahora, si casi todos los hosts fallan por la red, se entra en «sin red»: no se suman fallos ni pausas, se
    prueba la conexión con un solo feed cada 15 s y, al volver, se ponen a cero y todo se reprograma repartido."""
    specs = [spec(f"f{k}", f"https://host{k}.example/rss") for k in range(12)]
    p, _, store = make(tmp_path, specs)
    t = [1000.0]
    p.clock = lambda: t[0]
    key = SimpleNamespace(host="x", port=443, ssl=True)
    caida = aiohttp.ClientConnectorError(key, OSError(11001, "getaddrinfo failed"))
    modo = {"falla": True}

    async def fake_poll(st):
        if modo["falla"]:
            raise caida

    p.poll = fake_poll
    for sp in specs[:9]:   # 9 hosts caídos: aún no es «sin red» (hacen falta 10)
        await p.sem.acquire()
        await p._poll(p.states[sp.id])
    assert p.offline_since is None and p.states["f0"].fails == 1
    await p.sem.acquire()
    await p._poll(p.states["f9"])
    assert p.offline_since == 1000.0 and p.summary()["totals"]["sin_red"] is True
    for _ in range(5):   # durante el corte, los fallos de red no suman
        await p.sem.acquire()
        await p._poll(p.states["f10"])
    assert p.states["f10"].fails == 0 and p.states["f10"].err == "Sin red en este PC"
    # el planificador solo deja salir una prueba cada 15 s (sin reloj real: el bucle se para tras unas vueltas)
    p.sched = feeds.Scheduler()
    for sp in specs:
        p.sched.push(sp.id, 0)
    sent = []

    async def contar(st):
        sent.append(st.spec.id)
        p.inflight.discard(st.spec.id)
        p.sem.release()
        p.gate.done(st.host)

    vueltas = {"n": 0}

    async def dormir(_s):
        vueltas["n"] += 1
        if vueltas["n"] >= 3:
            p._closed = True
        await asyncio.sleep(0)

    async def ronda(ahora):
        t[0], vueltas["n"], p._closed = ahora, 0, False
        await p._dispatch()
        await asyncio.sleep(0)

    p._poll, p.sleep = contar, dormir
    p.next_probe = 1010.0
    await ronda(1005.0)
    assert sent == []          # aún no toca probar: todos esperan a la prueba
    await ronda(1026.0)        # pasada la hora de la prueba (y la de todos los que esperaban)
    assert len(sent) == 1      # una sola prueba
    assert p.offline_since == 1000.0 and p.next_probe == 1041.0 and len(p.sched) == 11
    # vuelve la red (la prueba responde): fallos y pausas de red a cero y todos reprogramados en el tiempo del arranque
    p.gate.next["host0.example"] = 5000.0
    t[0] = 1050.0
    p._net_result("host0.example", False)
    assert p.offline_since is None and p.offline_episodes == 1
    assert all(p.states[s.id].fails == 0 for s in specs) and not p.gate.fails and not p.gate.next
    assert len(p.sched) == 12 and all(1050.0 <= due <= 1050.0 + p.start_spread_s for due, _, _ in p.sched.heap)
    store.close()


def test_error_de_red_frente_a_respuesta_del_servidor():
    key = SimpleNamespace(host="x", port=443, ssl=True)
    assert feeds.is_network_error(aiohttp.ClientConnectorError(key, OSError(1, "x")))
    assert feeds.is_network_error(asyncio.TimeoutError()) and feeds.is_network_error(aiohttp.ServerDisconnectedError())
    assert not feeds.is_network_error(aiohttp.ClientResponseError(None, (), status=503))
    assert not feeds.is_network_error(feeds.FeedError("Respuesta sin RSS ni Atom"))
