"""Fuentes vivas: reglas con contracción, casos independientes y mínimos, medida ajustada al mercado, cobertura del
triaje, medio frente a canal, cambios reversibles con protección que caduca, tope de silencios, salud técnica y
descubrimiento de medios con una web falsa (sin red)."""
import asyncio
import json
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from bot import fuentes_vivas as fvmod
from bot.bus import Bus
from bot.feeds import FeedPoller, FeedSpec, load_registry
from bot.fuentes_vivas import (DAY, H, Cifras, Fila, FuentesVivas, Tecnico, contraido, decidir, enlaces_rss, espera,
                               prioridad_triaje, verificar_feed)
from bot.models import now_ms, story_id
from bot.store import Store

COLS = "story, ts, feed, medio, nombre, host, region, lang, tier, lat_s, veredicto, confirmada, medir_en"
_CURSOR: Counter = Counter()   # (base, feed) → ms ya ocupados hacia atrás: cada noticia de un feed, en su propia hora

GN_URL = "https://news.google.com/rss/search?q=bitcoin&ceid=JP:ja"


def spec(fid, url, region="jp", tier="T2", kind="rss", lang="ja") -> FeedSpec:
    return FeedSpec(fid, fid.upper(), url, lang, "JP", region, kind, tier, None)


class FakeDesk:
    def __init__(self):
        self.calls = []

    def handle(self, item, src, warm):
        self.calls.append(item)
        return "captured"


def make(tmp_path, specs, **kw):
    store = Store(tmp_path / "t.db")
    bus = Bus(store)
    poller = FeedPoller(bus, FakeDesk(), None, store, specs)
    poller.parse_workers = 0
    fv = FuentesVivas(bus, store, None, registro_dir=tmp_path / "fuentes", **kw)
    fv.attach(poller)
    return fv, poller, bus, store


def poner(store, feed, n, veredicto, *, medio=None, dias=2, confirmada=0, nombre=None, host=None, tag="", paso=7 * H,
          tier="T2", hasta=None):
    """n noticias ya medidas (cerradas) de un feed, desde hace unos días hacia atrás: cada una `paso` antes que la
    anterior (por defecto 7 h: cada noticia es un caso independiente y 30 caen en más de 7 días). Con `hasta` (ms) se
    cuentan hacia atrás desde ese instante fijo y no desde «ahora menos `dias`»: hace falta cuando la prueba depende
    de en qué hora o en qué día UTC caen (las cifras agrupan por horas y días UTC)."""
    ref = now_ms() - dias * DAY if hasta is None else hasta
    key = (str(store.path), feed)
    rows = []
    for i in range(n):
        _CURSOR[key] += paso
        rows.append((f"s-{feed}-{medio}-{veredicto}-{tag}{_CURSOR[key]}", ref - _CURSOR[key], feed, medio,
                     nombre, host, "jp", "ja", tier, 30.0, veredicto, confirmada, None))
    store.db.executemany(f"INSERT INTO fv_noticias ({COLS}) VALUES ({','.join('?' * 13)})", rows)
    store.db.commit()


def cif(a=0, f=0, r=0, u=0) -> Cifras:
    """Cada caso en su propia hora y su propio día (casos independientes)."""
    c, i = Cifras(), 0
    for v, n in (("acierto", a), ("fallo", f), ("ruido", r), ("util", u)):
        for _ in range(n):
            c.add(v, 1, 0, 1.0, 1, hora=i * 24)
            i += 1
    return c


def rss(n=5, web="https://nuevo.example", horas=2, title="Nuevo Diario") -> bytes:
    now = datetime.now(timezone.utc)
    items = "".join(f"<item><title>Titular {i}</title><link>{web}/art/{i}</link><guid>g{i}</guid>"
                    f"<pubDate>{format_datetime(now - timedelta(hours=horas + i))}</pubDate></item>" for i in range(n))
    return (f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>{title}</title>{items}'
            "</channel></rss>").encode()


class FakeWeb:
    """fetch falso: url → (estado, cuerpo). Apunta cada petición."""

    def __init__(self, pages):
        self.pages, self.calls = pages, []

    async def __call__(self, url):
        self.calls.append(url)
        status, body = self.pages.get(url, (404, b""))
        return status, url, body


async def no_sleep(_s):
    return None


# ---------- Reglas (funciones puras) ----------
def test_contraccion_y_minimo_de_casos():
    m, sd = contraido(0, 0)
    assert m == 0.5   # sin datos: neutro
    assert contraido(3, 0)[0] == pytest.approx(13 / 23)   # 3 de 3 no es un 100 %: 57 %
    fila = Fila("f", "activa")
    d = decidir(fila, cif(a=9, f=21), 0.5, now_ms())
    assert d[0] == "silenciada" and d[1] == "acierto" and "38 %" in d[2] and "30 casos independientes" in d[2]
    assert decidir(fila, cif(a=10, f=20), 0.5, now_ms()) is None      # 33 % en 30: no es «claramente» peor
    assert decidir(fila, cif(a=5, f=24), 0.5, now_ms()) is None       # 29 casos: aún no se concluye
    assert decidir(fila, cif(a=40, f=60), 0.5, now_ms())[1] == "acierto"   # 40 % con 100 casos sí


def test_las_noticias_de_una_hora_son_un_solo_caso():
    """30 noticias alcistas en un día de caída: 0 de 30, pero comparten mercado. Es UN caso, no treinta."""
    now = now_ms()
    fila = Fila("m", "activa")
    una_hora = Cifras()
    una_hora.add("fallo", 30, 0, 1.0, 1, hora=1000)
    assert (una_hora.casos, una_hora.direccion, una_hora.indep_dir, una_hora.dias_dir) == (30, 30, 1, 1)
    assert una_hora.acierto()[0] == pytest.approx(10 / 21)            # contraído como 1 caso: casi neutro
    assert decidir(fila, una_hora, 0.5, now) is None
    un_dia = Cifras()   # 30 horas distintas pero en 2 días: un mal par de días tampoco decide
    for h in range(30):
        un_dia.add("fallo", 1, 0, 1.0, 1, hora=2000 + h)
    assert (un_dia.indep_dir, un_dia.dias_dir) == (30, 2) and decidir(fila, un_dia, 0.5, now) is None
    ruido = Cifras()
    for h in range(40):
        ruido.add("ruido", 1, 0, 1.0, 1, hora=3000 + h)
    assert decidir(fila, ruido, 0.5, now) is None                     # 40 horas de ruido en 2 días: aún no
    for d in range(40):
        un_dia.add("fallo", 1, 0, 1.0, 1, hora=5000 + d * 24)
    assert decidir(fila, un_dia, 0.5, now)[1] == "acierto"             # repartido en semanas, sí


def test_ruido_oficiales_y_manual():
    now = now_ms()
    fila = Fila("f", "activa")
    assert decidir(fila, cif(r=30), 0.5, now) is None                 # 30 de 30, contraído al 88 %: aún no
    d = decidir(fila, cif(r=50), 0.5, now)
    assert d[:2] == ("silenciada", "ruido") and "92 %" in d[2]
    assert decidir(fila, cif(r=50), 0.5, now, tier="T0") is None      # oficial: solo técnica o a mano
    assert decidir(fila, cif(a=2, f=40), 0.5, now, tier="T0") is None
    assert decidir(fila, cif(a=2, f=40), 0.5, now, por_acierto=False) is None   # un feed (canal) no se juzga por acierto
    assert decidir(fila, cif(r=50), 0.5, now, por_acierto=False)[1] == "ruido"  # por ruido sí


def test_proteccion_a_mano_caduca_a_los_30_dias():
    now = now_ms()
    reciente = Fila("f", "activa", desde=now - 10 * DAY, manual=True)
    assert decidir(reciente, cif(a=2, f=40), 0.5, now) is None        # lo decidiste tú hace 10 días
    assert "20 días más" in espera(reciente, None, now)
    caida = Tecnico(12, 80, False, "HTTP 503")
    assert decidir(reciente, cif(), 0.5, now, tec=caida)[1] == "tecnico"   # una caída técnica cuenta siempre
    vieja = replace(reciente, desde=now - 31 * DAY)
    d = decidir(vieja, cif(a=2, f=40), 0.5, now)
    assert d[:2] == ("silenciada", "acierto") and "ya no la protege" in d[2]
    s = Fila("f", "silenciada", desde=now - 31 * DAY, tipo="manual", manual=True)
    assert decidir(s, cif(u=20), 0.5, now)[:2] == ("activa", "mejora")   # tras 30 días mandan los datos desde tu cambio
    assert decidir(replace(s, desde=now - 5 * DAY), cif(u=20), 0.5, now) is None


def test_prueba_se_activa_sin_malos_resultados():
    now = now_ms()
    p = Fila("n", "prueba")
    assert decidir(p, cif(u=19), 0.5, now) is None
    d = decidir(p, cif(u=20), 0.5, now)
    assert d[:2] == ("activa", "prueba") and "20 casos independientes" in d[2] and "20 noticias" in d[2]
    assert decidir(p, cif(a=4, f=8, u=8), 0.5, now) is None            # 12 con dirección y acierto 44 %: sigue en prueba
    assert decidir(p, cif(a=7, f=5, u=8), 0.5, now)[0] == "activa"
    assert decidir(p, cif(a=2, f=30), 0.5, now)[:2] == ("silenciada", "acierto")   # una prueba mala también se silencia
    vieja = Fila("n", "prueba", desde=now - 61 * DAY)
    d = decidir(vieja, cif(u=7), 0.5, now)
    assert d[:2] == ("silenciada", "pocos_datos") and "61 días" in d[2] and "7 noticias" in d[2] and "publica poco" in d[2]
    assert decidir(Fila("n", "prueba", desde=now - 59 * DAY), cif(u=7), 0.5, now) is None
    a_mano = Fila("n", "prueba", desde=now - 10 * DAY, manual=True)   # la pusiste tú en prueba: 30 días sin tocarla
    assert all(decidir(a_mano, c, 0.5, now) is None for c in (cif(u=7), cif(a=1, f=40), cif(u=20)))
    restaurable = Fila("n", "silenciada", desde=now - DAY, tipo="pocos_datos", previo="prueba")
    assert decidir(restaurable, cif(u=20), 0.5, now)[:2] == ("prueba", "mejora")   # si luego da noticias, vuelve


def test_prueba_sin_triaje_no_es_pocos_datos():
    """Publicó en 40 horas distintas pero la IA local solo trió 5: la falta de datos no es culpa del medio."""
    now = now_ms()
    vieja = Fila("n", "prueba", desde=now - 61 * DAY)
    c = cif(u=5)
    for h in range(35):
        c.add(None, 1, 0, 1.0, 1, hora=9000 + h, maduras=1)
    assert (c.indep, len(c.horas_cap), c.cobertura()) == (5, 40, pytest.approx(5 / 40))
    assert decidir(vieja, c, 0.5, now) is None
    assert espera(vieja, c, now).startswith("Sin triaje suficiente") and "cobertura del triaje 12 %" in espera(vieja, c, now)


def test_silenciada_restaurar_y_retirar():
    now = now_ms()
    s = Fila("f", "silenciada", desde=now - 5 * DAY, tipo="acierto", motivo="Acierta poco", previo="prueba")
    assert decidir(s, cif(a=6, f=4, u=15), 0.5, now) is None           # tras silenciar por acierto pide 20 con dirección
    d = decidir(s, cif(a=12, f=8), 0.5, now)
    assert d[:2] == ("prueba", "mejora")                               # vuelve a lo que era (prueba)
    assert decidir(s, cif(a=8, f=12), 0.5, now) is None
    viejo = Fila("f", "silenciada", desde=now - 31 * DAY, tipo="ruido", motivo="Casi todo es ruido")
    d = decidir(viejo, cif(r=50), 0.5, now)                            # en su silencio sigue siendo ruido: se retira
    assert d[:2] == ("retirada", "tiempo") and "31 días" in d[2] and "Casi todo es ruido" in d[2]
    assert decidir(Fila("f", "silenciada", desde=now - 40 * DAY, tipo="manual", manual=True), cif(), 0.5, now) is None
    assert decidir(Fila("f", "retirada"), cif(a=100), 0.5, now) is None   # retirada: solo a mano
    ruido = Fila("f", "silenciada", desde=now - DAY, tipo="ruido")
    assert decidir(ruido, cif(u=20), 0.5, now)[0] == "activa"


def test_retirar_solo_si_se_confirma_o_por_falta_de_datos_dicha_asi():
    now = now_ms()
    s = Fila("m", "silenciada", desde=now - 31 * DAY, tipo="acierto", motivo="Acierta poco")
    assert decidir(s, cif(a=2, f=5), 0.5, now) is None                 # 31 días sin datos: no se retira por eso
    assert espera(s, cif(a=2, f=5), now).startswith("Sin datos suficientes para decidir")
    d = decidir(s, cif(a=5, f=35), 0.5, now)
    assert d[:2] == ("retirada", "tiempo") and "Sigue igual tras 31 días" in d[2] and "Acierta poco" in d[2]
    viejo = replace(s, desde=now - 91 * DAY)
    d = decidir(viejo, cif(a=2, f=5), 0.5, now)
    assert d[:2] == ("retirada", "sin_datos") and "91 días" in d[2] and "no por malos resultados" in d[2]
    assert decidir(replace(viejo, tipo="pocos_datos"), cif(u=3), 0.5, now) is None   # nunca sola por falta de datos
    sin_triaje = cif(a=2, f=3)
    for h in range(35):
        sin_triaje.add(None, 1, 0, 1.0, 1, hora=9000 + h, maduras=1)
    assert decidir(viejo, sin_triaje, 0.5, now) is None                # dio material, pero no se trió
    assert espera(viejo, sin_triaje, now).startswith("Sin triaje suficiente para decidir")


def test_reglas_tecnicas():
    now = now_ms()
    fila = Fila("f", "activa")
    caido = Tecnico(12, 80, False, "HTTP 503")
    d = decidir(fila, cif(), 0.5, now, tier="T0", tec=caido)
    assert d[:2] == ("silenciada", "tecnico") and "12 fallos" in d[2] and "3,3 días" in d[2] and "HTTP 503" in d[2]
    assert decidir(fila, cif(), 0.5, now, tec=Tecnico(12, 60, False)) is None       # menos de 72 h
    assert decidir(fila, cif(), 0.5, now, tec=Tecnico(5, 100, False)) is None       # pocos fallos
    assert decidir(fila, cif(), 0.5, now, tec=caido, red_caida=True) is None         # es nuestra red
    s = Fila("f", "silenciada", desde=now - 40 * DAY, tipo="tecnico", previo="activa")
    assert decidir(s, cif(), 0.5, now, tec=Tecnico(0, 0, True))[:2] == ("activa", "mejora")
    assert decidir(s, cif(), 0.5, now, tec=Tecnico(3, 0, None)) is None              # sin sondeo aún: no se sabe
    d = decidir(s, cif(), 0.5, now, tec=caido)
    assert d[:2] == ("retirada", "tecnico") and "sigue caída" in d[2]   # no cuenta en el tope de silencios
    assert decidir(s, cif(), 0.5, now, tec=Tecnico(2, 5, False)) is None             # falla, pero no tanto: espera


def test_acierto_ajustado_al_mercado():
    """En días de caída (la tendencia acierta «bajista» 9 de cada 10 veces) fallar con noticias alcistas no dice que
    el medio sea malo: ajustado, queda cerca de lo neutro y no se silencia; sin ajustar, sí lo parecería."""
    now = now_ms()
    caida, neutro = Cifras(), Cifras()
    for i in range(40):
        caida.add("fallo", 1, 0, 1.0, 1, hora=i * 24, ac=0.0, base=0.1)   # alcista en día bajista
        neutro.add("fallo", 1, 0, 1.0, 1, hora=i * 24, ac=0.0, base=0.5)
    assert caida.acierto()[0] == pytest.approx((0.4 * 40 + 10) / 60)
    assert decidir(Fila("m", "activa"), caida, 0.5, now) is None
    assert decidir(Fila("m", "activa"), neutro, 0.5, now)[1] == "acierto"
    facil = Cifras()   # acertar «bajista» cuando todo cae no tiene mérito: 100 % bruto, 60 % ajustado
    for i in range(40):
        facil.add("acierto", 1, 0, 1.0, 1, hora=i * 24, ac=1.0, base=0.9)
    v = facil.view(0.5)
    assert v["acierto_bruto"] == 1.0 and v["base_mercado"] == 0.9 and v["acierto"] == pytest.approx((24 + 10) / 60, abs=1e-3)


def test_cupo_de_triaje_para_silenciadas_y_pruebas():
    ids = [story_id("jp-x", f"g{i}") for i in range(400)]
    assert all(prioridad_triaje(4.0, "activa", s) == 4.0 for s in ids) and prioridad_triaje(4.0, None, "x") == 4.0
    sil = [prioridad_triaje(4.0, "silenciada", s) for s in ids]
    assert set(sil) == {4.0 + fvmod.BONO_MUESTRA, 4.0 + fvmod.PRIO_SILENCIADA}
    assert 60 <= sil.count(4.0 + fvmod.BONO_MUESTRA) <= 140                       # ~1 de cada 4
    assert [prioridad_triaje(4.0, "silenciada", s) for s in ids] == sil           # fija por su id
    assert set(prioridad_triaje(4.0, "prueba", s) for s in ids) == {4.0, 4.0 + fvmod.BONO_MUESTRA}
    assert prioridad_triaje(4.0, "retirada", ids[0]) is None


# ---------- Medida ----------
def gnews_rss(entries) -> bytes:
    now = datetime.now(timezone.utc)
    items = "".join(
        f"<item><title>{t} - {medio}</title><link>https://news.google.com/rss/articles/{g}?oc=5</link>"
        f'<guid isPermaLink="false">{g}</guid><pubDate>{format_datetime(now - timedelta(minutes=3))}</pubDate>'
        f'<source url="{web}">{medio}</source></item>' for t, g, medio, web in entries)
    return f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>{items}</channel></rss>'.encode()


async def test_observar_volcar_y_medir(tmp_path):
    fv, poller, bus, store = make(tmp_path, [spec("jp-gn", GN_URL, kind="google_news")],
                                  veredictos=lambda ids: {story_id("jp-gn", "G3"): "acierto", "otra": "acierto"})
    body = gnews_rss([("Bitcoin sube", "G1", "Nikkei Asia", "https://asia.nikkei.com"),
                      ("ETF aprobado", "G2", "Nikkei Asia", "https://asia.nikkei.com"),
                      ("Yen cae", "G3", "Kabutan", "https://kabutan.jp")])

    async def fake_get(_st):
        return 200, body, None, None

    poller._get = fake_get
    await poller.poll(poller.states["jp-gn"])
    assert len(fv._buf) == 3 and fv.volcar() == 3 and fv._buf == []
    rows = store.db.execute("SELECT story, feed, medio, nombre, host, region, lang, tier, veredicto, medir_en - ts "
                            "FROM fv_noticias ORDER BY story").fetchall()
    by = {r[0]: r for r in rows}
    s1, s2, s3 = (story_id("jp-gn", g) for g in ("G1", "G2", "G3"))
    assert by[s1][1:9] == ("jp-gn", "nikkei.com", "Nikkei Asia", None, "jp", "ja", "T2", None)
    assert by[s3][2] == "kabutan.jp" and all(r[9] == pytest.approx(6 * H, abs=5000) for r in rows)
    # La mesa y la IA local hicieron su parte: s1 archivada, s2 triada y confirmada dos veces después.
    for kind, sid in (("lowprio", s1), ("triaged", s2), ("corroborated", s2), ("corroborated", s2)):
        bus.publish({"type": "event", "kind": kind, "agent": "local", "story": sid, "title": "x", "text": "", "coins": [],
                     "patch": {}})
    assert fv._medir(now_ms()) == 0                    # aún no tocan (6 h)
    assert fv._medir(now_ms() + 7 * H) == 3
    got = {s: (v, c, m is not None, t, ac) for s, v, c, m, t, ac in
           store.db.execute("SELECT story, veredicto, confirmada, medir_en, triada, ac FROM fv_noticias").fetchall()}
    # s3: del aprendizaje inyectado (sin triaje: no cuenta como triada)
    assert got == {s1: ("ruido", 0, True, 1, None), s2: ("util", 2, True, 1, None), s3: ("acierto", 0, True, 0, 1.0)}
    assert fv._medir(now_ms() + 100 * H) == 3          # última medida: quedan cerradas
    assert store.db.execute("SELECT COUNT(*) FROM fv_noticias WHERE medir_en IS NOT NULL").fetchone()[0] == 0
    store.close()


async def test_aprendizaje_roto_no_para_la_medida(tmp_path):
    def roto(_ids):
        raise RuntimeError("sin datos")

    fv, poller, bus, store = make(tmp_path, [spec("jp-a", "https://a.jp/rss")], veredictos=roto)
    store.db.execute(f"INSERT INTO fv_noticias ({COLS}) VALUES ('s1', ?, 'jp-a', 'a.jp', NULL, NULL, 'jp', 'ja', 'T2', 1.0, "
                     "NULL, 0, ?)", (now_ms() - 7 * H, now_ms() - H))
    store.db.commit()
    bus.publish({"type": "event", "kind": "triaged", "agent": "local", "story": "s1", "title": "x", "text": "",
                 "coins": [], "patch": {}})
    assert fv._medir(now_ms()) == 1
    assert store.db.execute("SELECT veredicto, triada FROM fv_noticias").fetchone() == ("util", 1)
    store.close()


def test_base_de_antes_gana_las_columnas_nuevas(tmp_path):
    """Una base creada con la versión anterior (sin triada, ac, base ni info) se amplía al arrancar."""
    store = Store(tmp_path / "t.db")
    store.db.executescript(f"CREATE TABLE fv_noticias(story TEXT PRIMARY KEY, ts INTEGER NOT NULL, feed TEXT NOT NULL, "
                           "medio TEXT, nombre TEXT, host TEXT, region TEXT, lang TEXT, tier TEXT, lat_s REAL, veredicto TEXT, "
                           "confirmada INTEGER NOT NULL DEFAULT 0, medir_en INTEGER);"
                           "CREATE TABLE fv_candidatos(medio TEXT PRIMARY KEY, nombre TEXT, region TEXT, lang TEXT, "
                           "host TEXT, estado TEXT NOT NULL, visto INTEGER, intento INTEGER, intentos INTEGER NOT NULL "
                           "DEFAULT 0, rss TEXT, feed TEXT, nota TEXT, cifras TEXT);")
    FuentesVivas(Bus(store), store, None, registro_dir=tmp_path / "fuentes")
    cols = {r[1] for r in store.db.execute("PRAGMA table_info(fv_noticias)").fetchall()}
    assert {"triada", "ac", "base"} <= cols
    assert "info" in {r[1] for r in store.db.execute("PRAGMA table_info(fv_candidatos)").fetchall()}
    store.close()


async def test_relays_dan_el_medio_por_su_enlace(tmp_path):
    fv, poller, bus, store = make(tmp_path, [spec("jp-a", "https://www.a.jp/rss")])
    for src, url in (("tree", "https://blog.nuevo.example/p/1"), ("tree", "https://x.com/alguien/status/1"),
                     ("tree", "https://www.a.jp/noticia"), ("tree", "https://www.coindesk.com/x"),
                     ("jp-a", "https://otro.example/x")):
        bus.publish({"type": "event", "kind": "captured", "agent": "hunter", "story": f"n-{url}", "title": "t", "text": "",
                     "coins": [], "patch": {"srcId": src, "url": url, "via": "Blogs", "region": None, "lang": "en",
                                            "tier": "T1", "lat": 5000}})
    # X no tiene RSS de medio y CoinDesk lo lee su cazador; a.jp sí cuenta aunque tenga feed propio (se juzga el medio
    # con todos sus canales)
    assert await fv.escanear_relays() == 2
    by = {row[3]: row for row in fv._buf}
    assert set(by) == {"nuevo.example", "a.jp"}
    assert by["nuevo.example"][2:7] == ("tree", "nuevo.example", "Blogs", "blog.nuevo.example", None)
    assert by["nuevo.example"][9] == 5.0
    assert await fv.escanear_relays() == 0   # no repite
    store.close()


async def test_veredictos_con_las_medidas_del_aprendizaje(tmp_path):
    from bot.aprendizaje import SCHEMA
    fv, poller, bus, store = make(tmp_path, [spec("jp-a", "https://a.jp/rss")])
    store.db.executescript(SCHEMA)
    t0 = now_ms() - 30 * H
    cols = "story, coin, outlet, rol, t0, archivada, dir, r60, r240, estado"
    rows = [("s1", "BTC", "a.jp", "primera", t0, 0, 1, 0.5, 0.4, "completa"),    # a favor
            ("s1", "ETH", "a.jp", "primera", t0, 0, 1, 0.3, 0.2, "completa"),
            ("s1", "BTC", "b.jp", "confirmacion", t0, 0, -1, 0.5, 0.4, "completa"),   # la confirmación no cuenta
            ("s2", "BTC", "a.jp", "primera", t0, 0, -1, 0.6, 0.2, "completa"),   # en contra
            ("s3", "BTC", "a.jp", "primera", t0, 1, 1, 2.0, 2.0, "completa"),    # archivada: ruido
            ("s4", "BTC", "a.jp", "primera", t0, 0, 0, 1.0, 1.0, "completa"),    # sin dirección prevista
            ("s5", "BTC", "a.jp", "primera", t0, 0, 1, 0.1, -0.1, "completa"),   # dentro del umbral
            ("s6", "BTC", "a.jp", "primera", t0, 0, 1, None, None, "pendiente"),  # aún sin medir: va el triaje
            ("b1", "SOL", "a.jp", "primera", t0, 0, -1, -0.5, -0.6, "completa"),  # bajista en día de caída: fácil
            ("u1", "SOL", "a.jp", "primera", t0, 0, 1, -0.5, -0.6, "completa")]   # alcista en día de caída
    store.db.executemany(f"INSERT INTO news_outcomes ({cols}) VALUES ({','.join('?' * 10)})", rows)
    # Ese día SOL se movió más del umbral 9 de cada 10 veces hacia abajo.
    store.db.execute("INSERT INTO mercado_base (coin, dia, s60, b60, s240, b240) VALUES ('SOL', ?, 0.05, 0.45, 0.05, 0.45)",
                     (t0 // DAY,))
    for s in ("s1", "s2", "s3", "s4", "s5", "s6", "s7", "b1", "u1"):
        store.db.execute(f"INSERT INTO fv_noticias ({COLS}) VALUES (?, ?, 'jp-a', 'a.jp', NULL, NULL, 'jp', 'ja', 'T2', 1.0, "
                         "NULL, 0, ?)", (s, t0, t0 + 24 * H))
    store.db.commit()
    bus.publish({"type": "event", "kind": "triaged", "agent": "local", "story": "s6", "title": "x", "text": "",
                 "coins": [], "patch": {}})
    assert fv._medir(now_ms()) == 9
    got = {s: (v, ac, b, t) for s, v, ac, b, t in
           store.db.execute("SELECT story, veredicto, ac, base, triada FROM fv_noticias").fetchall()}
    assert {s: v for s, (v, *_) in got.items()} == {"s1": "acierto", "s2": "fallo", "s3": "ruido", "s4": "util",
                                                    "s5": "util", "s6": "util", "s7": None, "b1": "acierto",
                                                    "u1": "fallo"}
    assert got["s1"][1:3] == (1.0, 0.5) and got["s2"][1:3] == (0.0, 0.5)          # sin base de mercado: 0,5
    assert got["b1"][1:3] == (1.0, pytest.approx(0.9)) and got["u1"][1:3] == (0.0, pytest.approx(0.1))
    assert got["s7"][3] == 0 and all(got[s][3] == 1 for s in ("s1", "s3", "s6", "b1"))
    # En las cifras, el acierto fácil cuenta poco y el fallo contra corriente casi nada.
    fv.cifras, fv.q0, _ = fv._calcular(now_ms(), {}, frozenset())
    c = fv.cifras["medio:a.jp"]
    assert (c.direccion, c.indep_dir) == (4, 1) and c.e == pytest.approx(2.0 * 0.5 ** (1 / 30), rel=0.05)
    store.close()


# ---------- Revisión completa y acciones de la web ----------
async def test_revision_silencia_el_medio_restaurar_a_mano_y_reinicio(tmp_path):
    specs = [spec("jp-malo", "https://malo.jp/rss"), spec("jp-bueno", "https://bueno.jp/rss"),
             spec("jp-oficial", "https://boj.or.jp/rss", tier="T0")]
    log_ev = []
    fv, poller, bus, store = make(tmp_path, specs, registrar=log_ev.append)
    poner(store, "jp-malo", 8, "acierto", medio="malo.jp")
    poner(store, "jp-malo", 27, "fallo", medio="malo.jp")
    poner(store, "jp-oficial", 5, "acierto", medio="boj.or.jp", tier="T0")
    poner(store, "jp-oficial", 30, "fallo", medio="boj.or.jp", tier="T0")
    poner(store, "jp-bueno", 25, "acierto", medio="bueno.jp")
    poner(store, "jp-bueno", 10, "fallo", medio="bueno.jp")
    res = await fv.revisar()
    assert [c["id"] for c in res["cambios"]] == ["medio:malo.jp"]
    assert poller.outlet_estados == {"malo.jp": "silenciada"}                  # vale para todos sus canales
    assert all(st.estado == "activa" for st in poller.states.values())         # los feeds no se juzgan por acierto
    assert poller.item_estado(poller.states["jp-malo"], "malo.jp") == "silenciada"
    [e] = log_ev
    assert (e["accion"], e["antes"], e["despues"], e["tipo"], e["auto"], e["deshacer"]) == \
        ("silenciar", "activa", "silenciada", "acierto", True, {"accion": "restaurar", "id": "medio:malo.jp"})
    cf = e["cifras"]
    assert (cf["casos"], cf["independientes"], cf["con_direccion"], cf["base_mercado"]) == (35, 35, 35, 0.5)
    assert cf["acierto"] < 0.45 and cf["acierto_techo90"] < 0.5 and cf["dias_dir"] >= 7 and cf["cobertura"] == 1.0
    msg = bus.state["fuentes_vivas"]
    assert msg["medios"]["silenciada"] == 1 and msg["cambios"][0]["id"] == "medio:malo.jp"
    assert msg["revision"]["cambios"] == 1 and "UN caso" in msg["aviso"] and "triaje" in msg["aviso"]
    top = fv.lista()[0]
    assert (top["id"], top["estado"], top["peso"], top["cambio"], top["acciones"]) == \
        ("medio:malo.jp", "silenciada", 0.0, "acierto", ["restaurar", "retirar"])
    assert "Acierta poco" in top["motivo"] and top["manual_hasta"] is None
    feed = {v["id"]: v for v in fv.lista(tipo="feed")}["jp-malo"]
    assert (feed["estado"], feed["medio"], feed["estado_medio"], feed["tecnico"]["fallos"]) == \
        ("activa", "malo.jp", "silenciada", 0)
    assert [v["id"] for v in fv.lista(estado="activa")] == ["medio:boj.or.jp", "medio:bueno.jp", "jp-bueno", "jp-malo",
                                                            "jp-oficial"]

    # Deshacer desde la web: vuelve a activa, a mano, y se respeta 30 días.
    assert fv.accion({"type": "fuente", "accion": "restaurar", "id": "medio:malo.jp"}) == \
        {"ok": True, "id": "medio:malo.jp", "antes": "silenciada", "estado": "activa"}
    assert poller.outlet_estados == {} and fv.filas["medio:malo.jp"].manual
    vista = {v["id"]: v for v in fv.lista(tipo="medio")}["medio:malo.jp"]
    assert vista["manual_hasta"] == fv.filas["medio:malo.jp"].desde + 30 * DAY and "30 días más" in vista["nota"]
    await fv.revisar()
    assert poller.outlet_estados == {} and len(log_ev) == 2
    h = fv.historial()
    assert [(x["accion"], x["por"]) for x in h] == [("restaurar", "tú, desde la web"), ("silenciar", "automático")]
    assert h[0]["deshacer"] == {"accion": "silenciar", "id": "medio:malo.jp"} and h[0]["texto"] == "Restaurada"
    r = fv.silenciar("medio:malo.jp", "  no me   fío ")
    assert r["ok"] and fv.filas["medio:malo.jp"].motivo == "Silenciada a mano: no me fío"
    assert fv.silenciar("medio:malo.jp")["error"] == "No se puede: está silenciada"
    assert fv.restaurar("jp-bueno")["ok"] is False and fv.restaurar("no-existe")["error"] == "No conozco esa fuente"
    assert fv.accion({"accion": "borrar", "id": "medio:malo.jp"})["ok"] is False
    assert fv.retirar("medio:malo.jp")["ok"] and poller.outlet_estados == {"malo.jp": "retirada"}
    assert fv.historial(clave="medio:malo.jp")[0]["deshacer"]["accion"] == "silenciar"   # volver a como estaba

    # Reinicio: el estado sale de la base y manda sobre el registro.
    p2 = FeedPoller(bus, FakeDesk(), None, store, specs)
    fv2 = FuentesVivas(bus, store, None, registro_dir=tmp_path / "fuentes")
    fv2.attach(p2)
    assert p2.outlet_estados == {"malo.jp": "retirada"} and fv2.filas["medio:malo.jp"].manual
    assert fv2.restaurar("medio:malo.jp")["estado"] == "activa"   # previo: activa
    store.close()


async def test_deshacer_vuelve_exactamente_al_estado_de_antes(tmp_path):
    fv, poller, bus, store = make(tmp_path, [spec("jp-n", "https://n.jp/rss", tier="T3")])
    fv._cambiar("jp-n", "prueba", "descubierta", "Medio nuevo", auto=True, por="descubrimiento")
    act = fv._cambiar("jp-n", "activa", "prueba", "Supera la prueba", auto=True, por="automático")
    assert act["accion"] == "activar" and act["deshacer"] == {"accion": "a_prueba", "id": "jp-n"}
    r = fv.accion(act["deshacer"])                    # deshacer la activación: vuelve a prueba, no a silenciada
    assert r == {"ok": True, "id": "jp-n", "antes": "activa", "estado": "prueba"}
    assert poller.states["jp-n"].estado == "prueba" and fv.historial()[0]["texto"] == "Vuelta a prueba"
    fv._cambiar("jp-n", "silenciada", "ruido", "Casi todo es ruido", auto=True, por="automático")
    ret = fv._cambiar("jp-n", "retirada", "tiempo", "Sigue igual", auto=True, por="automático")
    assert ret["deshacer"] == {"accion": "silenciar", "id": "jp-n"}
    assert fv.accion(ret["deshacer"])["estado"] == "silenciada"   # deshacer la retirada: silenciada, como estaba
    assert fv.restaurar("jp-n")["estado"] == "prueba"             # y al restaurar, a lo de antes de silenciarse
    assert fv.activar("jp-n")["estado"] == "activa" and fv.a_prueba("jp-n")["estado"] == "prueba"
    assert fv.a_prueba("jp-n")["error"] == "No se puede: está prueba"
    # Lo que haces a mano se respeta 30 días; luego las reglas vuelven a mirar (con los datos desde tu cambio).
    fila = fv.filas["jp-n"]
    assert fila.manual and fila.protegida(now_ms()) and not fila.protegida(now_ms() + 31 * DAY)
    store.close()


async def test_auto_apagado_solo_sugiere(tmp_path):
    fv, poller, bus, store = make(tmp_path, [spec("jp-malo", "https://malo.jp/rss")], auto=False)
    poner(store, "jp-malo", 2, "acierto", medio="malo.jp")
    poner(store, "jp-malo", 40, "fallo", medio="malo.jp")
    res = await fv.revisar()
    assert res["cambios"] == [] and poller.outlet_estados == {} and fv.historial() == []
    v = {x["id"]: x for x in fv.lista()}["medio:malo.jp"]
    assert v["sugerencia"]["estado"] == "silenciada" and "Acierta poco" in v["sugerencia"]["motivo"]
    assert bus.state["fuentes_vivas"]["auto"] is False and bus.state["fuentes_vivas"]["sugerencias"] == 1
    store.close()


async def test_consulta_prolifica_en_un_mal_dia_no_silencia_al_medio(tmp_path):
    """El caso del informe: una consulta de Google News capta 30 noticias alcistas de un medio en un día de caída
    (0 de 30). Comparten mercado: es un caso, no treinta, y nadie se silencia. La media hora va a una hora fija de un
    día UTC (11:30-11:59 de anteayer): contada desde «ahora menos dos días», entre las 00:01 y las 00:30 UTC cruzaba la
    medianoche y daba 2 días (ese caso lo fija la prueba siguiente)."""
    fv, poller, bus, store = make(tmp_path, [spec("jp-gn", GN_URL, kind="google_news")])
    anteayer = (now_ms() // DAY - 2) * DAY                                         # 00:00 UTC
    poner(store, "jp-gn", 30, "fallo", medio="prolifico.example", paso=60_000, hasta=anteayer + 12 * H)  # media hora
    poner(store, "jp-gn", 40, "fallo", medio="dos-dias.example", paso=H, dias=5)  # 40 horas seguidas
    res = await fv.revisar()
    assert res["cambios"] == [] and poller.outlet_estados == {} and fv.sugerencias == {}
    c = fv.cifras["medio:prolifico.example"]
    assert c.casos == 30 and c.indep == 1 and c.dias == 1
    c2 = fv.cifras["medio:dos-dias.example"]
    assert c2.indep == 40 and c2.dias <= 3
    store.close()


async def test_media_hora_que_cruza_la_medianoche_utc_tampoco_silencia(tmp_path):
    """Las horas y los días de las cifras son UTC de calendario (como mercado_base): la misma media hora de 23:45 a
    00:14 UTC son 2 casos en 2 días distintos. Sigue lejos de los mínimos (30 casos en 7 días) y nadie se silencia."""
    fv, poller, bus, store = make(tmp_path, [spec("jp-gn", GN_URL, kind="google_news")])
    anteayer = (now_ms() // DAY - 2) * DAY                                         # 00:00 UTC
    poner(store, "jp-gn", 30, "fallo", medio="prolifico.example", paso=60_000, hasta=anteayer + 15 * 60_000)
    res = await fv.revisar()
    assert res["cambios"] == [] and poller.outlet_estados == {} and fv.sugerencias == {}
    c = fv.cifras["medio:prolifico.example"]
    assert (c.casos, c.indep, c.dias, c.indep_dir, c.dias_dir) == (30, 2, 2, 2, 2)
    store.close()


async def test_tope_de_silencios_automaticos(tmp_path, monkeypatch):
    """Un mal periodo de mercado no puede silenciar muchas fuentes de golpe: 3 por revisión (y 10 al día); el resto
    queda como sugerencia, con lo más claro primero."""
    fv, poller, bus, store = make(tmp_path, [spec("jp-gn", GN_URL, kind="google_news")])
    for i in range(5):
        poner(store, "jp-gn", i, "acierto", medio=f"m{i}.example")
        poner(store, "jp-gn", 35, "fallo", medio=f"m{i}.example")
    res = await fv.revisar()
    assert [c["id"] for c in res["cambios"]] == ["medio:m0.example", "medio:m1.example", "medio:m2.example"]
    sug = {k: v for k, v in fv.sugerencias.items()}
    assert set(sug) == {"medio:m3.example", "medio:m4.example"} and all("Queda como sugerencia" in v[2] for v in sug.values())
    assert bus.state["fuentes_vivas"]["sugerencias"] == 2
    monkeypatch.setattr(fvmod, "MAX_BAJADAS_DIA", 4)
    res = await fv.revisar()
    assert [c["id"] for c in res["cambios"]] == ["medio:m3.example"] and set(fv.sugerencias) == {"medio:m4.example"}
    assert fv._bajadas_24h(now_ms()) == 4
    store.close()


async def test_medio_se_juzga_con_todos_sus_canales(tmp_path):
    specs = [spec("jp-gn", GN_URL, kind="google_news"), spec("jp-a", "https://www.a.jp/r"),
             spec("jp-gn2", "https://news.google.com/rss/search?q=precio&ceid=JP:ja", kind="google_news")]
    fv, poller, bus, store = make(tmp_path, specs)
    poner(store, "jp-gn", 3, "acierto", medio="bulos.example")
    poner(store, "jp-gn", 35, "fallo", medio="bulos.example")
    poner(store, "jp-gn", 40, "acierto", medio="bueno.example")
    poner(store, "jp-a", 20, "fallo", medio="a.jp")
    poner(store, "jp-gn", 20, "fallo", medio="a.jp")      # a.jp también llega por Google News
    for i in range(10):                                   # una consulta que solo trae ruido, de medios sueltos
        poner(store, "jp-gn2", 10, "ruido", medio=f"r{i}.example")
    await fv.revisar()
    assert poller.outlet_estados == {"bulos.example": "silenciada", "a.jp": "silenciada"}
    assert poller.states["jp-a"].estado == "activa"       # su feed propio es un canal: el medio manda en sus noticias
    assert poller.item_estado(poller.states["jp-a"], "a.jp") == "silenciada"
    assert poller.states["jp-gn"].estado == "activa"      # la consulta mezcla medios: nunca se juzga por acierto
    assert (poller.states["jp-gn2"].estado, fv.filas["jp-gn2"].tipo) == ("silenciada", "ruido")   # por ruido, sí
    a = {v["id"]: v for v in fv.lista(tipo="feed")}["jp-a"]
    assert (a["estado"], a["medio"], a["estado_medio"]) == ("activa", "a.jp", "silenciada")
    medios = fv.lista(tipo="medio")
    assert [m["id"] for m in medios][:3] == ["medio:a.jp", "medio:bulos.example", "medio:bueno.example"]
    assert medios[0]["cifras"]["casos"] == 40 and medios[0]["peso"] == 0.0
    assert fv.restaurar("medio:bulos.example")["ok"] and poller.outlet_estados == {"a.jp": "silenciada"}
    store.close()


def test_orden_a_mano_solo_para_fuentes_conocidas(tmp_path):
    """«medio:x» solo vale si x es un dominio y ya se ha visto (noticias, feed propio, estado en los feeds, catálogo del
    aprendizaje o candidato): una cadena cualquiera no crea una fuente silenciada inventada en el historial."""
    fv, poller, bus, store = make(tmp_path, [spec("jp-a", "https://www.a.jp/r")])
    poner(store, "jp-a", 1, "util", medio="visto.example")
    store.db.execute("INSERT INTO fv_candidatos (medio, estado, intentos) VALUES ('candidato.example', 'pendiente', 0)")
    store.db.execute("CREATE TABLE IF NOT EXISTS medios_info (outlet TEXT PRIMARY KEY)")
    store.db.execute("INSERT INTO medios_info VALUES ('catalogo.example')")
    store.db.commit()
    poller.set_outlet_estado("agregado.example", "prueba")
    for fid in ("medio:visto.example", "medio:candidato.example", "medio:catalogo.example", "medio:a.jp",
                "medio:agregado.example", "jp-a"):
        assert fv.silenciar(fid, "a mano")["ok"], fid
    for fid in ("medio:inventado.invalid", "medio:no es dominio", "medio:x", "medio:", "nadie", "MEDIO:visto.example"):
        assert fv.silenciar(fid) == {"ok": False, "error": "No conozco esa fuente"}, fid
        assert fid not in fv.filas
    assert store.db.execute("SELECT COUNT(*) FROM source_status_log").fetchone()[0] == 6
    store.close()


async def test_contadores_de_medios_con_los_medidos_y_sin_el_resto_de_prueba(tmp_path):
    """Hallazgos n.º 9 y 34: un medio medido sin ningún cambio de estado cuenta como activo (la web daba «0 medios
    activos» con miles medidos) y un dominio .invalid (el resto de una prueba) no cuenta ni sale en la lista, aunque su
    fila y su historial siguen ahí."""
    fv, poller, bus, store = make(tmp_path, [spec("jp-a", "https://www.a.jp/r")])
    for medio in ("uno.example", "dos.example", "tres.example"):
        poner(store, "jp-a", 3, "util", medio=medio)
    await fv.revisar()
    assert fv.estado()["medios"] == {"activa": 3, "prueba": 0, "silenciada": 0, "retirada": 0}
    fv._cambiar("medio:tres.example", "silenciada", "manual", "a mano", auto=False, por="tú, desde la web")
    fv._cambiar("medio:no-existe-prueba.invalid", "silenciada", "manual", "Silenciada a mano: prueba", auto=False,
                por="tú, desde la web")
    assert fv.estado()["medios"] == {"activa": 2, "prueba": 0, "silenciada": 1, "retirada": 0}
    ids = [v["id"] for v in fv.lista(tipo="medio")]
    assert "medio:tres.example" in ids and not any(i.endswith(".invalid") for i in ids)
    assert "medio:no-existe-prueba.invalid" in fv.filas
    assert store.db.execute("SELECT COUNT(*) FROM source_status_log WHERE id LIKE '%.invalid'").fetchone()[0] == 1
    store.close()


async def test_prueba_con_material_sin_triar_sigue_en_prueba(tmp_path):
    specs = [spec("jp-n", "https://n.jp/rss", tier="T3"), spec("jp-p", "https://p.jp/rss", tier="T3")]
    fv, poller, bus, store = make(tmp_path, specs)
    for fid in ("jp-n", "jp-p"):
        fv._cambiar(fid, "prueba", "descubierta", "Medio nuevo", auto=True, por="descubrimiento")
        fv.filas[fid] = replace(fv.filas[fid], desde=now_ms() - 61 * DAY)
    poner(store, "jp-n", 5, "util", medio="n.jp")
    poner(store, "jp-n", 55, None, medio="n.jp")          # 55 captadas que la IA local no llegó a triar
    poner(store, "jp-p", 5, "util", medio="p.jp")         # esta solo publicó 5 de interés
    await fv.revisar()
    assert poller.states["jp-n"].estado == "prueba"
    n = {v["id"]: v for v in fv.lista(tipo="feed")}["jp-n"]
    assert n["nota"].startswith("Sin triaje suficiente") and n["cifras"]["cobertura"] == pytest.approx(5 / 60, abs=1e-3)
    assert (poller.states["jp-p"].estado, fv.filas["jp-p"].tipo) == ("silenciada", "pocos_datos")
    assert "publica poco" in fv.filas["jp-p"].motivo
    store.close()


async def test_caida_tecnica_y_red_caida(tmp_path):
    fv, poller, bus, store = make(tmp_path, [spec(f"jp-{i}", f"https://h{i}.jp/rss") for i in range(5)])
    for st in poller.states.values():
        st.on_success(1)
    caido = poller.states["jp-0"]
    for _ in range(12):
        caido.on_error("HTTP 503")
    t = now_ms()
    assert fv._salud(t) is False and fv.salud == {"jp-0": 0.0}          # primer fallo visto de la racha
    assert fv._salud(t + 10 * H) is False and fv.salud == {"jp-0": pytest.approx(10)}
    # Hallazgo n.º 5: el tiempo es real. Con el bot parado 62 h entre medias, al volver (y si sigue fallando) cuenta todo.
    assert fv._salud(t + 72 * H) is False
    assert fv.salud == {"jp-0": pytest.approx(72)}
    assert store.db.execute("SELECT horas FROM source_health WHERE id='jp-0'").fetchone()[0] == pytest.approx(72)
    [c] = fv.aplicar_reglas(t)
    assert (c["id"], c["despues"], c["tipo"]) == ("jp-0", "silenciada", "tecnico") and "HTTP 503" in c["motivo"]
    assert caido.next_wait() == 7200   # silenciada y con espera por errores: se sigue probando, más despacio
    caido.on_success(1)                 # vuelve
    fv._salud(t + 75 * H)
    assert fv.salud == {}
    [c] = fv.aplicar_reglas(t)
    assert (c["despues"], c["tipo"]) == ("activa", "mejora")
    # Si falla casi todo a la vez es nuestra conexión: no se cuenta.
    for st in list(poller.states.values())[:3]:
        st.on_error("Sin conexión con el servidor")
    assert fv._salud(t + 77 * H) is True and fv.salud == {}
    store.close()


async def test_caida_tecnica_cuenta_el_tiempo_real_tras_reiniciar(tmp_path):
    """Hallazgo n.º 5: la racha sobrevive a un reinicio (source_health) y cuenta el tiempo real desde su primer fallo
    visto; un feed que funcionaba al parar empieza de cero aunque falle al volver. Las filas de antes (horas de bot en
    marcha) dan un inicio más tardío: se cuenta de menos, nunca de más."""
    fv, poller, bus, store = make(tmp_path, [spec(f"jp-{i}", f"https://h{i}.jp/rss") for i in range(10)])
    for st in poller.states.values():
        st.on_success(1)
    for _ in range(12):
        poller.states["jp-0"].on_error("HTTP 503")
    t = now_ms()
    fv._salud(t - 30 * H)
    store.db.execute("INSERT INTO source_health VALUES ('jp-9', 20.0, ?)", (t - 10 * H,))   # fila de antes
    store.db.commit()
    fv2 = FuentesVivas(bus, store, None, registro_dir=tmp_path / "fuentes")   # el bot se reinicia 40 h después
    fv2.attach(poller)
    assert fv2.salud_inicio == {"jp-0": t - 30 * H, "jp-9": t - 30 * H}
    for _ in range(12):
        poller.states["jp-1"].on_error("HTTP 500")                               # funcionaba al parar
    assert fv2._salud(t + 42 * H) is False
    assert fv2.salud["jp-0"] == pytest.approx(72) and fv2.salud["jp-1"] == 0.0 and "jp-9" not in fv2.salud
    [c] = fv2.aplicar_reglas(t + 42 * H)
    assert (c["id"], c["despues"], c["tipo"]) == ("jp-0", "silenciada", "tecnico") and "3,0 días" in c["motivo"]
    store.close()


# ---------- Descubrimiento ----------
def portada(*links: str) -> bytes:
    tags = "".join(links)
    return f"<html><head><title>Portada</title>{tags}</head><body>Hola</body></html>".encode()


async def test_descubre_verifica_y_anade_en_prueba(tmp_path):
    specs = [spec("jp-gn", GN_URL, kind="google_news"), spec("jp-nhk", "https://www3.nhk.or.jp/rss/news/cat0.xml")]
    log_ev = []
    fv, poller, bus, store = make(tmp_path, specs, registrar=log_ev.append)
    poner(store, "jp-gn", 8, "util", medio="nuevo.example", nombre="Nuevo Diario")
    poner(store, "jp-gn", 4, "util", medio="nuevo.example", nombre="Nuevo Diario", confirmada=1, tag="c")
    poner(store, "jp-gn", 12, "util", medio="tarde.example")                  # nunca llega antes
    poner(store, "jp-gn", 100, "ruido", medio="ruidoso.example", confirmada=1)  # llega antes, pero es ruido
    poner(store, "jp-gn", 20, "util", medio="x.com", confirmada=1)             # red social: sin RSS de medio
    poner(store, "jp-gn", 20, "util", medio="nhk.or.jp", confirmada=1)          # ya tiene feed propio
    web = FakeWeb({
        "https://nuevo.example/robots.txt": (200, b"User-agent: *\nDisallow: /privado\n"),
        "https://nuevo.example/": (200, portada('<link rel="alternate" type="application/rss+xml" '
                                                'title="Comentarios" href="/comments/feed/">',
                                                "<link rel='alternate' type='application/rss+xml' href='/noticias/rss.xml'>")),
        "https://nuevo.example/noticias/rss.xml": (200, rss()),
    })
    fv.fetch, fv.sleep = web, no_sleep
    res = await fv.revisar()
    [added] = res["anadidas"]
    fid = added["feed"]
    assert fid.startswith("jp-nuevo-example-") and added["url"] == "https://nuevo.example/noticias/rss.xml"
    assert web.calls == ["https://nuevo.example/robots.txt", "https://nuevo.example/",
                         "https://nuevo.example/noticias/rss.xml"]
    st = poller.states[fid]
    assert (st.estado, st.peso, st.spec.name, st.spec.region, st.spec.lang, st.spec.tier) == \
        ("prueba", 0.5, "Nuevo Diario", "jp", "ja", "T3")
    f = fv.filas[fid]
    assert (f.estado, f.origen, f.tipo, f.manual) == ("prueba", "descubierta", "descubierta", False)
    assert "12 noticias medidas" in f.motivo and "12 casos independientes" in f.motivo
    assert "4 antes que nadie" in f.motivo and "jp-gn" in f.motivo
    assert log_ev[-1]["accion"] == "anadir" and log_ev[-1]["antes"] is None and log_ev[-1]["deshacer"]["accion"] == "retirar"
    # Queda en el registro de su región, con su origen, y load_registry lo lee.
    [entry] = json.loads((tmp_path / "fuentes" / "jp.json").read_text(encoding="utf-8"))
    assert (entry["id"], entry["origen"], entry["tier"], entry["medio"]) == (fid, "descubierta", "T3", "nuevo.example")
    assert [s.id for s in load_registry(tmp_path / "fuentes")] == [fid]
    cands = {c["medio"]: c for c in fv.candidatos()}
    assert set(cands) == {"nuevo.example"}   # ni el ruidoso, ni el que llega tarde, ni X, ni NHK
    assert (cands["nuevo.example"]["estado"], cands["nuevo.example"]["feed"]) == ("anadido", fid)
    assert cands["nuevo.example"]["cifras"]["primeras"] == 4 and cands["nuevo.example"]["acciones"] == []
    assert bus.state["fuentes_vivas"]["candidatos"]["anadido"] == 1 and fv.historial()[0]["texto"] == "Añadida en prueba"
    assert poller.outlet_estados.get("ruidoso.example") == "silenciada"   # medio de agregador que solo mete ruido
    await fv.revisar()
    assert len(web.calls) == 3   # ya está: no se vuelve a pedir nada
    # Tras un reinicio sin el JSON (borrado o sin escribir), el feed descubierto sale de la base.
    (tmp_path / "fuentes" / "jp.json").unlink()
    p2 = FeedPoller(bus, FakeDesk(), None, store, specs)
    FuentesVivas(bus, store, None, registro_dir=tmp_path / "fuentes").attach(p2)
    assert p2.states[fid].estado == "prueba" and p2.states[fid].spec.url == added["url"]
    store.close()


async def test_auto_apagado_verifica_pero_no_da_de_alta(tmp_path):
    """Con BOT_FUENTES_AUTO=0 se busca y se verifica el RSS, pero no se añade nada ni se escribe el registro: queda
    «verificado» hasta que lo añades tú. Si luego se enciende el automático, lo verificado entra sin volver a pedirlo."""
    fv, poller, bus, store = make(tmp_path, [spec("jp-gn", GN_URL, kind="google_news")], auto=False)
    for medio in ("nuevo.example", "otro.example"):
        poner(store, "jp-gn", 8, "util", medio=medio, nombre=medio.split(".")[0].capitalize())
        poner(store, "jp-gn", 4, "util", medio=medio, confirmada=1, tag="c")
    web = FakeWeb({f"https://{m}/{p}": (200, body) for m in ("nuevo.example", "otro.example")
                   for p, body in (("robots.txt", b""), ("", portada()), ("feed", rss(web=f"https://{m}")))})
    fv.fetch, fv.sleep = web, no_sleep
    res = await fv.revisar()
    assert res["anadidas"] == [] and set(poller.states) == {"jp-gn"} and fv.historial() == []
    assert not (tmp_path / "fuentes").exists()
    cands = {c["medio"]: c for c in fv.candidatos()}
    c = cands["nuevo.example"]
    assert (c["estado"], c["rss"], c["feed"], c["acciones"]) == ("verificado", "https://nuevo.example/feed", None, ["anadir"])
    assert "Pendiente de que lo añadas" in c["nota"] and c["estado_texto"].startswith("RSS verificado")
    msg = bus.state["fuentes_vivas"]
    assert msg["candidatos"]["verificado"] == 2 and msg["sugerencias"] == 2
    n = len(web.calls)
    await fv.revisar()
    assert len(web.calls) == n   # verificado: no se vuelve a pedir
    # Lo añades tú desde la web.
    r = fv.accion({"type": "fuente", "accion": "anadir", "id": "medio:nuevo.example", "motivo": "me interesa"})
    assert r["ok"] and r["estado"] == "prueba"
    while fv._tareas:   # el JSON del registro se escribe en un hilo
        await asyncio.sleep(0.01)
    fid = r["feed"]
    f = fv.filas[fid]
    assert (poller.states[fid].estado, f.tipo, f.manual) == ("prueba", "descubierta", True)
    assert f.motivo.startswith("Añadido a mano: me interesa") and fv.historial()[0]["por"] == "tú, desde la web"
    assert [e["id"] for e in json.loads((tmp_path / "fuentes" / "jp.json").read_text(encoding="utf-8"))] == [fid]
    assert fv.anadir("nuevo.example")["ok"] is False and fv.anadir("nadie.example")["error"] == "No conozco ese medio"
    # Se enciende el automático: el otro, ya verificado, entra sin pedir nada más.
    fv.auto = True
    res = await fv.revisar()
    assert [a["medio"] for a in res["anadidas"]] == ["otro.example"] and len(web.calls) == n
    assert {c["medio"]: c["estado"] for c in fv.candidatos()} == {"nuevo.example": "anadido", "otro.example": "anadido"}
    store.close()


async def test_descubrimiento_respeta_robots_bloqueos_y_presupuesto(tmp_path, monkeypatch):
    fv, poller, bus, store = make(tmp_path, [spec("jp-gn", GN_URL, kind="google_news")])
    for medio, prim in (("c.example", 9), ("d.example", 8), ("e.example", 7), ("f.example", 6)):
        poner(store, "jp-gn", 6, "util", medio=medio)
        poner(store, "jp-gn", prim, "util", medio=medio, confirmada=1, tag="c")
    web = FakeWeb({
        "https://c.example/robots.txt": (200, b"User-agent: BotFundamental\nDisallow: /\n"),
        "https://d.example/": (403, b"Forbidden"),
        "https://e.example/": (200, portada()),
        "https://e.example/feed": (200, rss(web="https://e.example")),
        "https://f.example/": (200, portada()),
    })
    fv.fetch, fv.sleep = web, no_sleep
    monkeypatch.setattr(fvmod, "PETICIONES_HORA", 9)
    await fv.revisar()
    assert web.calls == ["https://c.example/robots.txt",                            # robots lo veta: ni la portada
                         "https://d.example/robots.txt", "https://d.example/",      # 403: bloqueo, no se insiste
                         "https://e.example/robots.txt", "https://e.example/", "https://e.example/rss",
                         "https://e.example/feed"]                                  # sin <link>: rutas típicas
    c = {x["medio"]: x for x in fv.candidatos()}
    assert (c["c.example"]["estado"], c["c.example"]["nota"]) == ("bloqueado", "robots.txt no deja leer la portada")
    assert (c["d.example"]["estado"], c["d.example"]["nota"]) == ("bloqueado", "La portada responde HTTP 403")
    assert c["e.example"]["estado"] == "anadido" and c["e.example"]["rss"] == "https://e.example/feed"
    assert (c["f.example"]["estado"], c["f.example"]["intentos"]) == ("pendiente", 0)   # sin presupuesto: a la próxima
    await fv.revisar()
    assert web.calls[7:] == ["https://f.example/robots.txt", "https://f.example/"] + \
        [f"https://f.example{r}" for r in fvmod.RUTAS_RSS]
    # Los bloqueados no se vuelven a pedir; f se queda sin RSS (6 pruebas como mucho) y no se reintenta hasta dentro de 7 días.
    assert {x["medio"]: x["estado"] for x in fv.candidatos()}["f.example"] == "sin_rss"
    n = len(web.calls)
    await fv.revisar()
    assert len(web.calls) == n
    store.close()


def test_verificar_feed():
    now = now_ms()
    ok, why, info = verificar_feed(rss(), now, "nuevo.example")
    assert ok and why.startswith("RSS válido: 5 entradas") and info["titulo"] == "Nuevo Diario" and info["del_medio"] == 5
    assert verificar_feed(b"<html><body>no</body></html>", now)[:2] == (False, "No es RSS ni Atom")
    assert verificar_feed(rss(n=2), now)[1] == "Solo 2 entradas"
    assert verificar_feed(rss(horas=24 * 9), now)[1] == "La última entrada es de hace 9 días"
    assert verificar_feed(rss(web="https://agregador.example"), now, "nuevo.example")[1] == \
        "El feed enlaza sobre todo a otros medios"
    atom = ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>A</title>' + "".join(
        f"<entry><title>T{i}</title><link href='https://a.example/{i}'/><id>{i}</id>"
        f"<updated>{(datetime.now(timezone.utc) - timedelta(hours=i)).isoformat()}</updated></entry>" for i in range(4))
        + "</feed>").encode()
    ok, _, info = verificar_feed(atom, now, "a.example")
    assert ok and info["tipo"] == "atom"


def test_enlaces_rss_de_la_portada():
    body = portada('<link rel="stylesheet" href="/a.css">',
                   '<LINK REL="alternate" TYPE="application/atom+xml" HREF="https://cdn.x.example/atom.xml">',
                   '<link rel="alternate" type="application/rss+xml" href="rss.php?cat=1&amp;lang=es">',
                   '<link rel="alternate" type="application/rss+xml" href="/comments/feed">',
                   '<link rel="alternate" hreflang="en" href="/en/">')
    assert enlaces_rss(body, "https://x.example/inicio/") == ["https://cdn.x.example/atom.xml",
                                                              "https://x.example/inicio/rss.php?cat=1&lang=es"]


async def test_run_no_se_cae(tmp_path):
    fv, poller, bus, store = make(tmp_path, [spec("jp-a", "https://a.jp/rss")])
    t, calls = [0.0], []

    async def fake_sleep(d):
        t[0] += d
        if len(calls) >= 2:
            fv._closed = True

    async def rota():
        calls.append(t[0])
        raise RuntimeError("boom")

    fv.clock, fv.sleep, fv.revisar = (lambda: t[0]), fake_sleep, rota
    await asyncio.wait_for(fv.run(), 5)
    assert calls == [120, 3720]   # falla, se registra y vuelve a la hora siguiente
    store.close()


async def test_cambios_al_historial_de_evolucion_y_deshacer_desde_alli(tmp_path):
    from bot import evolucion
    fv, poller, bus, store = make(tmp_path, [spec("jp-a", "https://a.jp/rss")])
    evo = evolucion.Evolucion(bus, store, archivo=tmp_path / "estrategia_activa.json")
    evo.conectar_fuentes(fv)
    assert fv.silenciar("jp-a", "pruebas")["ok"]
    [e] = evolucion.leer(store, origen="fuentes_vivas")
    assert (e["tipo"], e["antes"], e["despues"]) == ("fuente_silenciada", {"estado": "activa", "peso": 1.0},
                                                     {"estado": "silenciada", "peso": 0.0})
    assert e["evidencia"]["deshacer"] == {"accion": "restaurar", "id": "jp-a"} and "pruebas" in e["motivo"]
    assert evo.reversores["fuentes_vivas"](e)["ok"] and poller.states["jp-a"].estado == "activa"
    last = evolucion.leer(store, origen="fuentes_vivas")[0]
    assert last["tipo"] == "fuente_restaurada" and last["revierte"] == e["id"]
    # Una activación (prueba → activa) se deshace con «a_prueba»: la evolución la registra con esa orden inversa.
    fv._cambiar("jp-a", "prueba", "descubierta", "alta", auto=True, por="descubrimiento")
    fv._cambiar("jp-a", "activa", "prueba", "Supera la prueba", auto=True, por="automático")
    act = evolucion.leer(store, origen="fuentes_vivas")[0]
    assert act["tipo"] == "peso_cambiado" and act["evidencia"]["deshacer"] == {"accion": "a_prueba", "id": "jp-a"}
    assert fv.accion(act["evidencia"]["deshacer"])["estado"] == "prueba"
    store.close()
