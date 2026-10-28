"""Aprendizaje (bot/aprendizaje.py) sin red: medida de cada noticia con velas sintéticas (desde el primer cierre tras
la captura), filas por historia, moneda y medio sin duplicados tras un reinicio, recuentos de ruido, acierto y peso por
medio con contracción bayesiana, casos independientes por bloques de 4 h y días distintos, decaimiento, población de la
señal, alias nombre → dominio, historial de pesos reversible y con histéresis, post-mortems con una IA local falsa (baja
prioridad), mensaje «aprendizaje», el medio de cada historia tras un reinicio y fallos que no paran nada."""
import json
import math
from datetime import datetime, timezone

import numpy as np
import pytest

from bot import aprendizaje as A
from bot import config
from bot.bus import Bus
from bot.store import Store

MIN, H, DAY = A.MIN, A.H, A.DAY
T = int(datetime(2025, 11, 11, 6, 0, tzinfo=timezone.utc).timestamp() * 1000)   # martes, 2 h antes de abrir Europa
EU_OPEN = T + 2 * H


def series(knots, t0=T - 3 * DAY, t1=T + 20 * DAY):
    """Velas de 1 min [t, o, h, l, c] que siguen la línea quebrada knots: el cierre de la vela que abre en t es el
    precio en t + 1 min (como en Binance), así el precio «en t» es el cierre de la vela anterior."""
    t = np.arange(t0, t1, MIN, dtype=np.int64)
    xs, ys = [k[0] for k in knots], [k[1] for k in knots]
    o = np.interp(t.astype(float), xs, ys)
    c = np.interp((t + MIN).astype(float), xs, ys)
    return np.column_stack([t, o, np.maximum(o, c), np.minimum(o, c), c])


def flat(t0=T - 3 * DAY, t1=T + 20 * DAY):
    return series([(t0, 100.0), (t1, 100.0)], t0, t1)


class FakeBars:
    """Fuente de velas en memoria con la interfaz de CacheBars (get y until); fail simula un disco roto."""

    def __init__(self, arrays, cap=None):
        self.a, self.cap, self.fail = arrays, cap, None

    def get(self, coin, a, b):
        if self.fail:
            raise self.fail
        x = self.a.get(coin, np.empty((0, 5)))
        return x[(x[:, 0] >= a) & (x[:, 0] < b)]

    def until(self, coin):
        if self.fail:
            raise self.fail
        x = self.a.get(coin)
        if x is None or not len(x):
            return None
        end = int(x[-1, 0]) + MIN
        return min(end, self.cap) if self.cap else end


def captured(story, t0, url="https://www.reuters.com/a", src="Reuters", src_id="us-reuters-1", via=None, tier="T1",
             region="us", lang="en", coins=("BTC",), headline="La Fed recorta los tipos", clase=None):
    ev = {"type": "event", "kind": "captured", "agent": "hunter", "story": story, "ts": t0, "title": headline,
          "coins": list(coins), "text": "",
          "patch": {"headline": headline, "src": src, "srcId": src_id, "via": via, "tier": tier, "t0": t0,
                    "lat": 2 * MIN, "url": url, "coins": list(coins), "pub": t0 - 2 * MIN, "region": region,
                    "lang": lang, "fresh": True, "topics": ["macro"], "filter": {"ok": True, "why": "macro"}}}
    if clase:
        ev["patch"]["clase"] = clase
    return ev


def reflejo_ev(story, ts, estado="confirmada"):
    """Evento «reflejo» de bot/reflejo.py: la cripto confirmó (o no) una macro."""
    return {"type": "event", "kind": "reflejo", "agent": "flow", "story": story, "ts": ts, "title": "", "coins": [],
            "text": "", "patch": {"reflejo": {"estado": estado, "mov_pct": 0.8, "min": 7, "ref": "media",
                                              "umbral_pct": 0.5}}}


def triaged(story, ts, direccion=60, activos=("BTC",), importancia=7, kind="triaged", cred=80, tipo="macro"):
    tr = {"tipo": tipo, "temas": ["tipos"], "credibilidad": cred, "alcance": {"nivel": "global", "puntuacion": 80},
          "temporalidad": {"horizonte": "horas", "urgencia": 70},
          "repercusion": {"direccion": direccion, "magnitud": 50, "activos": list(activos), "bolsas": []},
          "novedad": 70, "importancia": importancia, "sentimiento": "alcista", "monedas": list(activos),
          "resumen": f"Resumen de {story}", "motivo": "Mueve el apetito por el riesgo"}
    return {"type": "event", "kind": kind, "agent": "local", "story": story, "ts": ts, "title": "t",
            "coins": list(activos), "text": "", "patch": {"triage": tr}}


def corroborated(story, ts, corrob):
    return {"type": "event", "kind": "corroborated", "agent": "hunter", "story": story, "ts": ts, "title": "",
            "coins": [], "text": "", "patch": {"corrob": corrob}}


def filtered(ts, story="nf1", url="https://www.reuters.com/b", src_id="us-reuters-1"):
    return {"type": "event", "kind": "filtered", "agent": "hunter", "story": story, "ts": ts, "title": "x", "coins": [],
            "text": "", "patch": {"headline": "x", "src": "Reuters", "srcId": src_id, "via": None, "tier": "T1", "t0": ts,
                                  "url": url, "region": "us", "lang": "en", "filter": {"ok": False, "why": "sin tema"}}}


BLOOMBERG = {"src": "GNews US · Bloomberg", "tier": "T1", "lat": 5 * MIN, "title": "Fed cuts",
             "url": "https://news.google.com/rss/articles/x", "region": "us"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_PESOS", {})
    monkeypatch.setattr(A, "_ALIAS", {})
    store = Store(tmp_path / "bot.db")
    bus = Bus(store)
    yield store, bus
    store.close()


def make(bus, store, bars, now, **kw):
    return A.Aprendizaje(bus, store, bars=bars, clock=lambda: now, **kw)


# ---------------------------------------------------------------- piezas puras

def test_medios_ventanas_y_reglas():
    assert A.outlet_from_patch({"via": "香港01", "url": "https://news.google.com/x", "srcId": "hk-gnews"}) == "香港01"
    assert A.outlet_from_patch({"url": "https://www.yna.co.kr/view/1", "srcId": "kr-x"}) == "yna.co.kr"
    assert A.outlet_from_patch({"outlet": "Reuters.com", "via": "Reuters"}) == "reuters.com"
    assert A.outlet_from_patch({"url": "https://news.google.com/x", "srcId": "kr-x"}) == "kr-x"
    # Tipo de clave y si llega directo: un feed de Google News sin subfuente es un agregador, no un medio directo.
    assert A.outlet_info({"url": "https://news.google.com/x", "srcId": "kr-x"}) == ("kr-x", "feed", 0)
    assert A.outlet_info({"via": "Bloomberg", "url": "https://news.google.com/x"}) == ("bloomberg", "nombre", 0)
    assert A.outlet_info({"via": "Investing.com", "url": "https://news.google.com/x"}) == ("investing.com", "dominio", 0)
    assert A.outlet_info({"url": "https://www.bloomberg.com/a", "srcId": "us-b"}) == ("bloomberg.com", "dominio", 1)
    assert A.outlet_from_corrob(BLOOMBERG) == "bloomberg"
    assert A.outlet_from_corrob({"src": "Yonhap", "url": "https://www.yna.co.kr/view/AKR"}) == "yna.co.kr"
    w = A.window_of(T)
    assert (w["ventana"], w["fase"], w["min"]) == ("europa", "previa", -120)
    assert A.window_of(T + 8 * H)["ventana"] == "nuevayork"   # 14:00 UTC: Nueva York abre a las 14:30 en noviembre
    s = A.window_of(T + 9 * H + 30 * MIN)                     # 15:30 UTC: una hora después de abrir Nueva York
    assert (s["ventana"], s["fase"]) == ("nuevayork", "sesion")
    sat = int(datetime(2025, 11, 15, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert A.window_of(sat) is None                           # sábado: fuera de ventana
    # La horquilla de medida respeta las reglas fijas: stop ≥ 1,5 %, ≥ 2R y objetivo ≥ 3 % neto (1,5 × 2R = 2,76 % neto).
    for over in ({"horquilla_stop_pct": 0.5, "horquilla_r": 1.0}, {"horquilla_stop_pct": 1.5, "horquilla_r": 2.0}):
        p = A._params(over)
        assert p["horquilla_stop_pct"] == config.RULE_MIN_STOP_PCT and p["horquilla_r"] > config.RULE_MIN_RR
        assert p["horquilla_stop_pct"] * p["horquilla_r"] - A.ROUNDTRIP_COST_PCT >= 3.0
    p = A._params({"horquilla_stop_pct": 2.0, "horquilla_r": 2.0})   # 4 % − 0,24 %: ya cumple, no se toca
    assert (p["horquilla_stop_pct"], p["horquilla_r"]) == (2.0, 2.0)
    assert A._params(None)["horquilla_r"] == 3.0


def test_medida_por_etapas_extremos_y_horquilla():
    # La base es el primer cierre posterior a la captura (t0 = T + 30 s → cierre de la vela de T, en T + 1 min).
    B = T + MIN
    knots = [(T - 3 * DAY, 100.0), (B, 100.0), (B + 60 * MIN, 101.0), (B + 240 * MIN, 102.5), (B + 600 * MIN, 88.0),
             (T + 20 * DAY, 88.0)]
    bars = series(knots)
    f = lambda t: float(np.interp(t, [k[0] for k in knots], [k[1] for k in knots]))
    row = {"t0": T + 30_000, "pub": T - 30 * MIN, "lat": 30 * MIN + 30_000, "fase": "previa", "apertura_ms": EU_OPEN}
    p = A._params(None)
    part = A.measure(bars, row, T + 90 * MIN, p)              # solo hay velas hasta +90 min
    assert part["estado"] == "pendiente" and part["medido"] == 60 and part["proxima"] == B + 240 * MIN
    assert part["r60"] == pytest.approx(1.0) and part["r240"] is None and part["horq_largo"] is None
    m = A.measure(bars, row, T + 2 * DAY, p)
    assert m["estado"] == "completa" and m["medido"] == 1440 and m["proxima"] is None
    assert m["p0"] == pytest.approx(100.0) and m["previo"] == pytest.approx(0.0) and m["desde_pub"] == pytest.approx(0.0)
    assert m["r15"] == pytest.approx((f(B + 15 * MIN) / 100 - 1) * 100, abs=1e-3)
    assert m["r240"] == pytest.approx(2.5) and m["r1440"] == pytest.approx(-12.0)
    assert m["max_sube"] == pytest.approx(2.5, abs=1e-3) and m["max_baja"] == pytest.approx(12.0, abs=1e-3)
    assert m["horq_largo"] == "stop" and m["horq_corto"] == "objetivo"   # stop 3 %, objetivo 9 %
    assert m["ap15"] == pytest.approx((f(EU_OPEN + 15 * MIN) / f(EU_OPEN) - 1) * 100, abs=1e-3)
    assert m["ap240"] == pytest.approx((f(EU_OPEN + 240 * MIN) / f(EU_OPEN) - 1) * 100, abs=1e-3)
    assert m["acc60"] == m["r60"] and m["acc240"] == m["r240"]   # sin hora de triaje: lo accionable es lo mismo
    # Triada 2 h después: lo accionable se mide desde el primer cierre tras el triaje.
    tt = T + 2 * H + 10_000
    ta = T + 2 * H + MIN
    m2 = A.measure(bars, {**row, "ts_triaje": tt}, T + 2 * DAY, p)
    assert m2["acc60"] == pytest.approx((f(ta + 60 * MIN) / f(ta) - 1) * 100, abs=1e-3) and m2["r60"] == m["r60"]
    late_tri = A.measure(bars, {**row, "ts_triaje": T + 23 * H}, T + DAY + 10 * MIN, p)   # 24 h medidas, triaje tardío
    assert late_tri["medido"] == 1440 and late_tri["estado"] == "pendiente" and late_tri["acc240"] is None
    late = series(knots, t0=T + DAY)                          # sin velas en t0: espera y, pasado el plazo, «sin_datos»
    assert A.measure(late, row, T + 2 * DAY, p)["estado"] == "pendiente"
    assert A.measure(late, row, T + 5 * DAY, p)["estado"] == "sin_datos"


def test_lo_de_antes_de_la_captura_no_cuenta():
    # El precio cae un 1 % en los 30 s anteriores a captar (dentro del mismo minuto): el bot no pudo operarlo.
    knots = [(T - 3 * DAY, 100.0), (T, 100.0), (T + 30_000, 99.0), (T + 20 * DAY, 99.0)]
    m = A.measure(series(knots), {"t0": T + 30_000}, T + 2 * DAY, A._params(None))
    assert m["p0"] == pytest.approx(99.0) and m["r15"] == pytest.approx(0.0) and m["max_baja"] == pytest.approx(0.0)
    assert m["horq_corto"] == "ninguna"


def test_horquilla_con_huecos_no_se_inventa():
    knots = [(T - 3 * DAY, 100.0), (T + 20 * DAY, 100.0)]
    bars = series(knots)
    p = A._params(None)
    row = {"t0": T + 30_000}
    assert A.measure(bars, row, T + 2 * DAY, p)["horq_largo"] == "ninguna"
    holed = bars[(bars[:, 0] < T + 5 * H) | (bars[:, 0] >= T + 6 * H)]   # una hora sin velas
    m = A.measure(holed, row, T + 2 * DAY, p)
    assert m["estado"] == "completa" and m["horq_largo"] is None and m["horq_corto"] is None
    # Con el stop tocado antes del hueco sí se sabe.
    drop = series([(T - 3 * DAY, 100.0), (T + MIN, 100.0), (T + 2 * H, 96.0), (T + 20 * DAY, 96.0)])
    drop = drop[(drop[:, 0] < T + 5 * H) | (drop[:, 0] >= T + 6 * H)]
    assert A.measure(drop, row, T + 2 * DAY, p)["horq_largo"] == "stop"


def test_contraccion_beta_y_peso():
    m, ic = A._beta(3, 0, 10)
    assert m == pytest.approx(8 / 13) and ic[0] < 0.5 < m      # 3 de 3 no demuestra nada
    m2, ic2 = A._beta(60, 20, 10)
    assert m2 == pytest.approx(65 / 90) and ic2[0] > 0.5
    assert A._beta(0, 0, 10)[0] == 0.5


def test_credibilidad_ajustada(monkeypatch):
    monkeypatch.setattr(A, "_PESOS", {"bueno.com": 1.5, "malo.com": 0.25})
    assert A.source_weight("Bueno.com ") == 1.5 and A.source_weight("otro.com") == 1.0 and A.source_weight(None) == 1.0
    assert A.adjusted_credibility(80, "bueno.com") == 100 and A.adjusted_credibility(80, "malo.com") == 20
    assert A.adjusted_credibility(80, "otro.com") == 80 and A.adjusted_credibility(None, "bueno.com") is None


def test_cache_de_velas(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HIST_DIR", tmp_path / "hist")
    d = tmp_path / "hist" / "1m"
    d.mkdir(parents=True)
    rows = [[T + k * MIN, 100, 101, 99, 100 + k, 1, 1, 0.5] for k in range(10)]
    (d / "BTCUSDT-1m-2025-11-api.json").write_text(json.dumps({"symbol": "BTCUSDT", "month": "2025-11", "rows": rows}))
    live = np.array([[T + k * MIN, 100, 101, 99, 200 + k, 1, 1] for k in range(8, 15)], float)
    cb = A.CacheBars(live={"BTC": live})
    x = cb.get("BTC", T, T + DAY)
    assert len(x) == 15 and x[8, 4] == 208 and x[3, 4] == 103   # lo de la vigilancia manda en los minutos repetidos
    assert cb.until("BTC") == T + 15 * MIN
    assert A.CacheBars().until("ETH") is None and len(A.CacheBars().get("ETH", T, T + DAY)) == 0


# ---------------------------------------------------------------- de los eventos a las filas

async def test_filas_recuentos_y_reinicio(env):
    store, bus = env
    up = series([(T - 3 * DAY, 100.0), (T + MIN, 100.0), (T + 61 * MIN, 101.0), (T + 20 * DAY, 101.0)])
    bars = FakeBars({"BTC": up, "ETH": flat(), "SOL": flat(), "XRP": flat()})
    for ev in (captured("n1", T), triaged("n1", T + MIN), filtered(T + 2 * MIN),
               captured("n2", T + 3 * MIN, url="https://otro.com/a", src="Otro", src_id="us-otro-1", coins=("ETH",)),
               corroborated("n1", T + 5 * MIN, [BLOOMBERG])):
        bus.publish(ev)
    now = T + 2 * DAY
    apr = make(bus, store, bars, now)
    await apr.step(now, force=True)
    db = store.db
    rows = db.execute("SELECT story, coin, outlet, rol, retraso, adelanto, fase, ventana, dir, peso, estado, r60 "
                      "FROM news_outcomes ORDER BY orden").fetchall()
    assert [r[:4] for r in rows] == [("n1", "BTC", "reuters.com", "primera"), ("n1", "BTC", "bloomberg", "confirmacion")]
    assert rows[0][4:10] == (0, 5 * MIN, "previa", "europa", 1, 1.0) and rows[1][4] == 5 * MIN and rows[1][5] is None
    assert rows[0][10] == "completa" and rows[0][11] == pytest.approx(1.0)
    cnt = {(r[0], r[1]): r[2:] for r in db.execute("SELECT clase, clave, SUM(captadas), SUM(filtradas), SUM(triadas), "
                                                   "SUM(archivadas), SUM(confirmaciones) FROM medios_conteo "
                                                   "GROUP BY clase, clave")}
    assert cnt[("medio", "reuters.com")] == (1, 1, 1, 0, 0) and cnt[("fuente", "us-reuters-1")] == (1, 1, 1, 0, 0)
    assert cnt[("medio", "otro.com")] == (1, 0, 0, 0, 0) and cnt[("medio", "bloomberg")] == (0, 0, 0, 0, 1)
    assert ("reuters.com", "us-reuters-1") in db.execute("SELECT outlet, fuente FROM medio_fuente").fetchall()
    assert db.execute("SELECT directo FROM medios_info WHERE outlet = 'bloomberg'").fetchone() == (0,)   # solo agregador
    st = db.execute("SELECT titular, resumen, outlet, cuerpo FROM news_stories WHERE story = 'n1'").fetchone()
    assert st[:3] == ("La Fed recorta los tipos", "Resumen de n1", "reuters.com")
    assert json.loads(st[3])["confirmaciones"][0]["medio"] == "bloomberg"
    m = apr.stats["medios"]["reuters.com"]
    assert m["principal"]["casos"] == 1 and m["peso"] == 1.0 and m["veredicto"] == "pocos_datos"
    assert m["ruido"]["piezas"] == 2 and m["ruido"]["fuera_de_tema"] == 0.5 and m["ruido"]["archivo"] == 0.0
    assert m["clave_tipo"] == "dominio" and m["directo"] and not m["candidato"]
    bl = apr.stats["medios"]["bloomberg"]   # nombre de Google News sin dominio conocido: ni directo ni candidato
    assert bl["clave_tipo"] == "nombre" and not bl["directo"] and not bl["candidato"]
    row = db.execute("SELECT senal, n_medios, ts_triaje FROM news_outcomes WHERE story = 'n1' AND rol = 'primera'").fetchone()
    # «La Fed recorta los tipos» es macro y la cripto no la ha confirmado: el motor no la usa (2), pero para el acierto
    # del medio cuenta como las demás (contar solo las confirmadas lo inflaría por construcción)
    assert row == (2, 2, T + MIN)

    # Reinicio: mismo cursor, ni filas ni recuentos repetidos.
    apr2 = make(bus, store, bars, now + MIN)
    await apr2.step(now + MIN, force=True)
    assert db.execute("SELECT COUNT(*) FROM news_outcomes").fetchone()[0] == 2
    assert db.execute("SELECT SUM(captadas), SUM(filtradas) FROM medios_conteo WHERE clase = 'medio' "
                      "AND clave = 'reuters.com'").fetchone() == (1, 1)

    # Triaje tardío de n2 y nuevo triaje de n1 que añade ETH: filas nuevas, la noticia global pesa 1/2 por moneda.
    bus.publish(triaged("n2", now + 90_000, activos=("ETH",), direccion=-40))   # después del cursor (now + 50 s)
    bus.publish(triaged("n1", now + 95_000, activos=("BTC", "ETH")))
    await apr2.step(now + 2 * MIN, force=True)
    n2 = db.execute("SELECT coin, outlet, dir FROM news_outcomes WHERE story = 'n2'").fetchall()
    assert n2 == [("ETH", "otro.com", -1)]
    n1 = db.execute("SELECT coin, outlet, peso, estado FROM news_outcomes WHERE story = 'n1' ORDER BY coin, orden").fetchall()
    assert [(c, o) for c, o, _, _ in n1] == [("BTC", "reuters.com"), ("BTC", "bloomberg"), ("ETH", "reuters.com"),
                                             ("ETH", "bloomberg")]
    assert all(pw == 0.5 for _, _, pw, _ in n1) and n1[0][3] == "completa"   # lo ya medido no se pierde
    assert db.execute("SELECT SUM(triadas) FROM medios_conteo WHERE clase = 'medio' AND clave = 'otro.com'").fetchone() == (1,)


async def test_medio_de_la_historia_tras_reinicio_y_noticia_fresca(env):
    store, bus = env
    bars = FakeBars({"BTC": flat()})
    bus.publish(captured("n1", T, url="https://www.bueno.com/a", src="Bueno", src_id="us-bueno"))
    bus.publish(triaged("n1", T + MIN))
    bus.publish(captured("n2", T + 2 * MIN, url="https://www.bueno.com/b", src="Bueno", src_id="us-bueno"))   # sin triar
    now = T + 2 * H
    apr = make(bus, store, bars, now)
    await apr.step(now, force=True)
    assert apr.fijar_peso("bueno.com", 1.5)["ok"]
    assert apr.weight_of_story("n1") == 1.5 and apr.weight_of_story("n2") == 1.5
    # Reinicio: el cursor guardado ya no relee esas captadas.
    A._PESOS.clear()
    apr2 = make(bus, store, bars, now + MIN)
    assert apr2.weight_of_story("n1") == 1.5               # de news_stories, ya en el constructor
    assert apr2.annotate([triaged("n1", now, cred=80)])[0]["credibilidad_ajustada"] == 100
    assert apr2.weight_of_story("n2") == 1.0               # sin triar: hasta la precarga de la primera ronda
    await apr2.step(now + MIN, force=True)
    assert apr2.weight_of_story("n2") == 1.5               # de las captadas de las últimas 48 h
    # Noticia fresca (aún no ingerida): el gancho observe la anota al momento; sin gancho vale el patch de la captada.
    fresh = captured("n3", now + MIN + 5_000, url="https://www.bueno.com/c", src="Bueno", src_id="us-bueno")
    assert apr2.weight_of_story("n3") == 1.0
    apr2.observe(fresh)
    assert apr2.weight_of_story("n3") == 1.5
    apr2.observe({"kind": "captured"})                     # nunca lanza
    apr2.observe(None)
    tri = triaged("n4", now, cred=80)
    tri["patch"] = {**fresh["patch"], **tri["patch"]}
    assert apr2.annotate([tri])[0]["credibilidad_ajustada"] == 100 and apr2.weight_of_story("n4", tri["patch"]) == 1.5


async def test_confirmaciones_no_cuentan_varias_veces_en_los_desgloses(env):
    store, bus = env
    up = series([(T - 3 * DAY, 100.0), (T + MIN, 100.0), (T + 61 * MIN, 101.0), (T + 20 * DAY, 101.0)])
    corrob = [{"src": f"Medio {k}", "tier": "T2", "lat": k * 35 * MIN, "title": "x", "url": f"https://www.m{k}.jp/a",
               "region": "jp"} for k in range(1, 6)]
    for ev in (captured("n1", T), triaged("n1", T + MIN), corroborated("n1", T + 3 * H, corrob),
               # otra noticia de m1.jp con su idioma: las confirmaciones no lo traen y sale del catálogo
               captured("n9", T + 4 * H, url="https://www.m1.jp/b", src="Medio 1", src_id="jp-m1", region="jp",
                        lang="ja")):
        bus.publish(ev)
    now = T + 3 * DAY
    apr = make(bus, store, FakeBars({"BTC": up}), now)
    await apr.step(now, force=True)
    assert apr.stats["global"]["principal"]["casos"] == 1
    tipo = {x["clave"]: x["principal"] for x in apr.estadisticas("tipo")}
    assert tipo["macro"]["casos"] == 1 and tipo["macro"]["independientes"] == 1
    niv = {x["clave"]: x["principal"] for x in apr.estadisticas("nivel")}
    assert sum(x["casos"] for x in niv.values()) == pytest.approx(1.0)
    assert niv["T1"]["casos"] == pytest.approx(1 / 6, abs=0.06) and niv["T2"]["casos"] == pytest.approx(5 / 6, abs=0.06)
    assert all(x["independientes"] == 1 for x in niv.values())   # por la hora de la historia, no de cada confirmación
    idi = {x["clave"]: x["principal"]["casos"] for x in apr.estadisticas("idioma")}   # (cifras redondeadas a 0,1)
    assert idi["ja"] == pytest.approx(1 / 6, abs=0.06) and idi["en"] == pytest.approx(1 / 6, abs=0.06)
    assert idi["–"] == pytest.approx(4 / 6, abs=0.06)   # m2-m5 sin idioma en el catálogo
    rol = {x["clave"]: x["principal"]["casos"] for x in apr.estadisticas("rol")}
    assert rol == {"primera": 1, "confirmacion": 5}


async def test_directo_y_google_news_del_mismo_medio_se_juntan(env):
    store, bus = env
    gn = "https://news.google.com/rss/articles/"
    for ev in (captured("d1", T, url="https://www.bloomberg.com/a", src="Bloomberg", src_id="us-bloomberg"),
               triaged("d1", T + MIN),
               captured("g1", T + 6 * H, url=gn + "x", src="GNews US · Bloomberg", src_id="us-gnews", via="Bloomberg"),
               triaged("g1", T + 6 * H + MIN),
               captured("g2", T + 12 * H, url=gn + "y", src="GNews US", src_id="us-gnews"),   # sin subfuente
               triaged("g2", T + 12 * H + MIN),
               captured("g3", T + 18 * H, url=gn + "z", src="GNews US · Investing.com", src_id="us-gnews",
                        via="Investing.com"),
               triaged("g3", T + 18 * H + MIN)):
        bus.publish(ev)
    now = T + 3 * DAY
    apr = make(bus, store, FakeBars({"BTC": flat()}), now)
    await apr.step(now, force=True)
    md = apr.stats["medios"]
    b = md["bloomberg.com"]
    assert "bloomberg" not in md and b["alias"] == ["bloomberg"] and b["principal"]["casos"] == 2
    assert b["directo"] and not b["candidato"] and b["ruido"]["captadas"] == 2
    feed = md["us-gnews"]   # Google News sin subfuente: un agregador, nunca un medio directo ni candidato
    assert feed["clave_tipo"] == "feed" and not feed["directo"] and not feed["candidato"]
    inv = md["investing.com"]
    assert inv["clave_tipo"] == "dominio" and not inv["directo"] and inv["candidato"]
    cands = [m["medio"] for m in apr.build_message(now)["medios_candidatos"]]
    assert cands == ["investing.com"]
    assert apr.medio("Bloomberg")["medio"] == "bloomberg.com"
    r = apr.fijar_peso("Bloomberg", 1.3)
    assert r["ok"] and r["outlet"] == "bloomberg.com"
    assert A.source_weight("bloomberg") == 1.3 == A.source_weight("bloomberg.com") and apr.weight_of_story("g1") == 1.3


async def test_solo_cuenta_lo_que_usaria_la_senal(env):
    store, bus = env

    def local(story, ts, bolsas=()):
        ev = triaged(story, ts, activos=())
        ev["patch"]["triage"].update(alcance={"nivel": "local", "puntuacion": 20}, tipo="empresas")
        ev["patch"]["triage"]["repercusion"]["bolsas"] = list(bolsas)
        return ev

    for ev in (captured("l1", T, coins=()), local("l1", T + MIN),                    # local de empresas: solo se muestra
               captured("l2", T + 6 * H, coins=()), local("l2", T + 6 * H + MIN, ("londres",)),   # nombra una bolsa
               captured("l3", T + 12 * H, coins=()),                                 # archivada (global)
               triaged("l3", T + 12 * H + MIN, kind="lowprio", importancia=2, activos=())):
        bus.publish(ev)
    now = T + 3 * DAY
    flat4 = {c: flat() for c in config.COINS}
    apr = make(bus, store, FakeBars(flat4), now)
    await apr.step(now, force=True)
    rows = {}
    for story, coin, senal in store.db.execute("SELECT story, coin, senal FROM news_outcomes ORDER BY story, coin"):
        rows.setdefault(story, []).append((coin, senal))
    assert rows["l1"] == [("BTC", 0)] and rows["l2"] == [("BTC", 2)]   # l2: macro sin confirmar (cuenta para el acierto)
    assert sorted(rows["l3"]) == sorted((c, 0) for c in config.COINS)
    m = apr.stats["medios"]["reuters.com"]
    assert m["publicaciones"] == 3 and m["archivadas"] == 1 and m["principal"]["casos"] == 1   # solo l2
    assert {x["story"] for x in await apr.noticias("mejores", 60, dias=60)} == {"l2"}   # por defecto, las que cuentan
    todas = await apr.noticias("mejores", 60, dias=60, solo_senal=False)
    assert any("no entra en la señal" in x["explicacion"] for x in todas if x["story"] == "l1")


def test_macro_sin_confirmar_no_entra_en_la_senal_pero_si_en_el_acierto():
    """Hallazgo n.º 14: in_signal aplica el reflejo como el motor (1 = la usa; 2 = macro que la cripto no confirmó: no
    la usa, pero cuenta para el acierto) y la macro de la clase nueva (captada sin monedas) se mide con las cuatro."""
    tr = triaged("x", T, activos=())["patch"]["triage"]
    tr["alcance"] = {"nivel": "nacional", "puntuacion": 40}
    ok = {"estado": "confirmada"}
    assert A.in_signal(tr, 0, "ETH", "La Fed recorta los tipos", "macro", None) == 2
    assert A.in_signal(tr, 0, "ETH", "La Fed recorta los tipos", "macro", ok) == 1
    assert A.in_signal(tr, 0, "ETH", "La Fed recorta los tipos", "macro", {"estado": "contraria"}) == 2
    assert A.in_signal(tr, 0, "ETH", "Bitcoin ETF", "cripto", None) == 0          # cripto sin monedas ni alcance
    assert A.in_signal(tr, 0, "ETH", "La Fed recorta los tipos", None, ok) == 0   # sin clase: la regla de siempre
    assert A.in_signal(tr, 1, "ETH", "x", "macro", ok) == 0                       # archivada
    assert A.story_coins(tr, {"clase": "macro", "coins": []}) == list(config.COINS)
    assert A.story_coins(tr, {"clase": "contexto", "coins": []}) == ["BTC"]


def test_la_senal_de_una_operacion_solo_usa_la_macro_confirmada_antes(tmp_path):
    """signal_news (aportaciones de una operación a sus noticias): como el motor, una macro solo si la cripto la confirmó
    antes de la entrada; la confirmada de la clase «macro» vale para las cuatro monedas."""
    store = Store(tmp_path / "t.db")
    store.db.executescript(A.SCHEMA)
    bus = Bus(store)
    p = A._params(None)
    for ev in (captured("m1", T, coins=(), clase="macro"), triaged("m1", T + MIN, activos=()),
               captured("c1", T, coins=("BTC",), clase="cripto", headline="Bitcoin ETF inflows"),
               triaged("c1", T + MIN)):
        bus.publish(ev)
    ids = lambda at, coin="BTC": sorted(x["story"] for x in A.signal_news(store.db, coin, at, p))
    assert ids(T + 30 * MIN) == ["c1"]
    bus.publish(reflejo_ev("m1", T + 20 * MIN))
    assert ids(T + 10 * MIN) == ["c1"] and ids(T + 30 * MIN) == ["c1", "m1"] and ids(T + 30 * MIN, "XRP") == ["m1"]
    store.close()


# ---------------------------------------------------------------- acierto, peso e historial

def scenario(outlets):
    """Historias cada 6 h: tras cada noticia (todas alcistas) BTC sube o baja un 1 % en 60 min según el medio. Los
    medios se alternan para que cada día haya subidas y bajadas (la base de mercado del día queda cerca del 50 %)."""
    queues = [[o] * n for o, n, _ in outlets]
    plan = []
    while any(queues):
        plan += [q.pop() for q in queues if q]
    moves = {o: mv for o, _, mv in outlets}
    knots, px, evs = [(T - 3 * DAY, 100.0)], 100.0, []
    for k, o in enumerate(plan):
        tk = T + k * 6 * H
        knots.append((tk, px))
        px *= 1 + moves[o] / 100
        knots.append((tk + 60 * MIN, px))
        sid = f"n{k:03d}"
        evs += [captured(sid, tk, url=f"https://www.{o}/{k}", src=o.split(".")[0].title(), src_id=f"us-{o}"),
                triaged(sid, tk + MIN)]
    knots.append((T + 20 * DAY, px))
    return series(knots), evs, len(plan)


async def test_pesos_contraccion_minimos_e_historial(env):
    store, bus = env
    btc, evs, n = scenario([("bueno.com", 30, +1), ("malo.com", 30, -1), ("pocos.com", 5, +1)])
    for ev in evs:
        bus.publish(ev)
    now = T + n * 6 * H + 2 * DAY
    apr = make(bus, store, FakeBars({"BTC": btc}), now)
    await apr.step(now, force=True)
    md = apr.stats["medios"]
    b, m, p = md["bueno.com"], md["malo.com"], md["pocos.com"]
    bp, mp = b["principal"], m["principal"]
    assert bp["casos"] == 30 and bp["independientes"] == 30 and bp["bruto"] == 1.0 and 0.8 < bp["p"] < 1.0
    assert 0.3 < bp["base"] < 0.7 and 0.7 < bp["p_aj"] < bp["p"]   # días con subidas y bajadas: base cerca del 50 %
    assert b["peso"] == 1.5 and b["veredicto"] == "acierta" and b["horizontes"]["60"]["ret"] > 0.5
    assert mp["bruto"] == 0.0 and mp["p_aj"] < 0.3 and m["peso"] < 0.5 and m["veredicto"] == "falla"
    # Pocos casos: 5 de 5 aciertos se contraen hacia el 50 % y no mueven el peso.
    assert p["principal"]["casos"] == 5 and 0.55 < p["principal"]["p"] < 0.75 and p["peso"] == 1.0
    assert p["veredicto"] == "pocos_datos" and "Pocos datos" in p["nota"]
    assert store.db.execute("SELECT COUNT(*) FROM mercado_base WHERE coin = 'BTC' AND completa = 1").fetchone()[0] >= 16
    assert A.source_weight("bueno.com") == 1.5 and A.source_weight("pocos.com") == 1.0
    assert A.adjusted_credibility(80, "malo.com") == round(80 * m["peso"])
    assert [x["medio"] for x in apr.medios()][:1] == ["bueno.com"] and apr.medios(orden="acierto")[-1]["medio"] == "malo.com"
    assert apr.estadisticas("moneda")[0]["clave"] == "BTC" and apr.estadisticas("nivel")[0]["clave"] == "T1"

    hist = store.db.execute("SELECT id, outlet, antes, despues, tipo, motivo FROM medios_peso_hist ORDER BY id").fetchall()
    assert {h[1] for h in hist} == {"bueno.com", "malo.com"}
    hb = next(h for h in hist if h[1] == "bueno.com")
    assert hb[2:5] == (1.0, 1.5, "auto") and "Acierto a 1-4 h" in hb[5] and "IC90" in hb[5]

    # Con los mismos datos no se apunta nada nuevo (histéresis).
    await apr.step(now + MIN, force=True)
    assert store.db.execute("SELECT COUNT(*) FROM medios_peso_hist").fetchone()[0] == len(hist)

    # A mano y reversible: deshacer lo fijado a mano lo devuelve al automático (no lo deja fijado para siempre);
    # deshacer esa reversión lo vuelve a fijar a mano, y el peso fijado sigue tras un reinicio.
    hm = next(h for h in hist if h[1] == "malo.com")
    r = apr.fijar_peso("malo.com", 1.0, "Lo pruebo neutro")
    assert A.source_weight("malo.com") == 1.0 and r["antes"] == m["peso"]
    back = apr.revertir(r["id"])
    assert back["tipo"] == "revertir" and A.source_weight("malo.com") == pytest.approx(m["peso"])
    assert apr.weights["malo.com"]["manual"] is None and f"n.º {r['id']}" in (await apr.cambios(1, "malo.com"))[0]["motivo"]
    again = apr.revertir(back["id"])
    assert again["despues"] == 1.0 and apr.weights["malo.com"]["manual"] == 1.0
    A._PESOS.clear()
    apr3 = make(bus, store, FakeBars({"BTC": btc}), now + 2 * MIN)
    assert A.source_weight("malo.com") == 1.0 and apr3.weights["malo.com"]["manual"] == 1.0
    assert apr3.fijar_peso("malo.com", None)["despues"] == pytest.approx(m["peso"])   # vuelve al automático
    assert apr3.revertir(hm[0])["despues"] == 1.0   # deshacer el cambio automático: fija el peso de antes (1)
    assert apr3.weights["malo.com"]["manual"] == 1.0
    cambios = await apr3.cambios(10, "malo.com")
    assert cambios[0]["tipo"] == "revertir" and len(cambios) == 6
    # Un apunte de veredicto no cambió el peso: no hay nada que revertir
    store.db.execute("INSERT INTO medios_peso_hist(ts, outlet, antes, despues, tipo, motivo, cuerpo) VALUES "
                     "(?, 'malo.com', 1.0, 1.0, 'veredicto', 'Falla (veredicto: neutro → falla)', '{}')", (now,))
    vid = store.db.execute("SELECT MAX(id) FROM medios_peso_hist").fetchone()[0]
    res = apr3.revertir(vid)
    assert res["ok"] is False and "veredicto" in res["why"] and apr3.revertir(10 ** 9) is None

    # Las que mejor y peor funcionaron, con su explicación en español.
    best = await apr.noticias("mejores", 60, dias=60, limit=3)
    worst = await apr.noticias("peores", 60, dias=60, limit=3)
    assert best[0]["outlet"] in ("bueno.com", "pocos.com") and "acertó" in best[0]["explicacion"]
    assert worst[0]["outlet"] == "malo.com" and "falló" in worst[0]["explicacion"]
    assert worst[0]["titular"] == "La Fed recorta los tipos"

    # La señal puede usar el peso por historia (credibilidad_ajustada, el campo que lee apertura._news).
    sid = next(e["story"] for e in evs if e["kind"] == "captured" and "bueno.com" in e["patch"]["url"])
    out = apr.annotate([triaged(sid, now, cred=80)])
    assert out[0]["credibilidad_ajustada"] == 100 and apr.weight_of_story(sid) == 1.5


INS = ("INSERT INTO news_outcomes (story, coin, outlet, rol, t0, dir, peso, r60, r240, estado) "
       "VALUES (?, ?, ?, 'primera', ?, ?, ?, ?, ?, 'completa')")
BASE = "INSERT INTO mercado_base VALUES ('BTC', ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.3, 1, 0)"


def test_decaimiento_independencia_y_una_historia_global_cuenta_una_vez(env):
    store, _ = env
    now = T + 200 * DAY
    A.Aprendizaje(Bus(store), store, bars=FakeBars({}), clock=lambda: now)   # crea las tablas
    db = store.db
    for k in range(30):   # cada 6 h: cada noticia en su bloque de 4 h, repartidas en 8 días
        db.execute(INS, (f"v{k}", "BTC", "viejo.com", now - 90 * DAY + k * 6 * H, 1, 1.0, 1.0, 1.0))
        db.execute(INS, (f"w{k}", "BTC", "nuevo.com", now - 10 * DAY + k * 6 * H, 1, 1.0, 1.0, 1.0))
        db.execute(INS, (f"x{k}", "BTC", "antiguo.com", now - 190 * DAY + k * H, 1, 1.0, 1.0, 1.0))   # fuera de mirar_d
        db.execute(INS, (f"y{k}", "BTC", "racha.com", now - DAY + k * 1000, 1, 1.0, 1.0, 1.0))   # todas en la misma hora
    for c in config.COINS:
        db.execute(INS, ("g1", c, "global.com", now - DAY, 1, 0.25, 1.0, 1.0))
    db.commit()
    res = A.compute_stats(db, now, A._params({"min_dias": 5}))
    md = res["medios"]
    v, n, r = md["viejo.com"]["principal"], md["nuevo.com"]["principal"], md["racha.com"]["principal"]
    assert v["casos"] == n["casos"] == r["casos"] == 30 and v["independientes"] == 30 and v["dias"] == 8
    assert 0.5 < v["p"] < n["p"]                             # lo viejo pesa menos
    assert r["independientes"] == 1 and 0.5 < r["p"] < 0.6   # 30 noticias de la misma hora: casi un solo caso
    assert md["racha.com"]["veredicto"] == "pocos_datos" and md["racha.com"]["peso_auto"] == 1.0
    assert "antiguo.com" not in md
    assert md["global.com"]["principal"]["casos"] == 1.0   # 4 monedas, una sola historia
    assert res["global"]["principal"]["casos"] == 91


def test_veinte_horas_seguidas_no_mueven_el_peso(env):
    """Un solo episodio: 20 noticias alcistas en 20 horas seguidas de una subida sostenida, con la base de mercado del
    día en el 50 %. Las ventanas de 4 h se solapan y es un mismo régimen: ni concluye ni mueve el peso."""
    store, _ = env
    now = T + 10 * DAY
    A.Aprendizaje(Bus(store), store, bars=FakeBars({}), clock=lambda: now)
    db = store.db
    t_start = (now - 3 * DAY) // DAY * DAY + 2 * H
    for k in range(20):
        db.execute(INS, (f"s{k}", "BTC", "episodio.com", t_start + k * H, 1, 1.0, 1.0, 1.5))
    for d in {(t_start + k * H) // DAY for k in range(20)}:
        db.execute(BASE, (d, 0.25, 0.25, 0.25, 0.25, 0.25, 0.25, 0.25, 0.25))
    db.commit()
    m = A.compute_stats(db, now, A._params(None))["medios"]["episodio.com"]
    pr = m["principal"]
    assert pr["casos"] == 20 and pr["independientes"] <= 6 and pr["dias"] <= 2
    assert m["veredicto"] == "pocos_datos" and m["peso_auto"] == 1.0 and "Pocos datos" in m["nota"]
    # Aunque bajes el mínimo de casos, sigue haciendo falta repartirlos en días distintos.
    m2 = A.compute_stats(db, now, A._params({"min_casos": 5}))["medios"]["episodio.com"]
    assert m2["veredicto"] == "pocos_datos" and m2["peso_auto"] == 1.0


def test_sin_evidencia_el_peso_no_se_mueve(env):
    """14 de 20 aciertos (base 50 %): media ajustada por encima del 50 %, pero el IC90 lo incluye → neutro y peso 1."""
    store, _ = env
    now = T + 40 * DAY
    A.Aprendizaje(Bus(store), store, bars=FakeBars({}), clock=lambda: now)
    db = store.db
    t_start = (now - 25 * DAY) // DAY * DAY
    for k in range(20):
        t = t_start + k * DAY + 8 * H
        r = 1.0 if k < 14 else -1.0
        db.execute(INS, (f"m{k}", "BTC", "medio.com", t, 1, 1.0, r, r))
        db.execute(BASE, (t // DAY, 0.25, 0.25, 0.25, 0.25, 0.25, 0.25, 0.25, 0.25))
    db.commit()
    m = A.compute_stats(db, now, A._params(None))["medios"]["medio.com"]
    pr = m["principal"]
    assert pr["independientes"] == 20 and pr["dias"] == 20 and pr["p_aj"] > 0.55 and pr["ic90_aj"][0] < 0.5
    assert m["veredicto"] == "neutro" and m["peso_auto"] == 1.0 and "peso 1" in m["nota"]


def test_extremos_y_horquilla_solo_con_24_h_completas(env):
    store, _ = env
    now = T + 10 * DAY
    A.Aprendizaje(Bus(store), store, bars=FakeBars({}), clock=lambda: now)
    ins = ("INSERT INTO news_outcomes (story, coin, outlet, rol, t0, dir, peso, r60, max_sube, max_baja, horq_largo, "
           "estado) VALUES (?, 'BTC', 'x.com', 'primera', ?, 1, 1.0, 0.5, ?, ?, ?, ?)")
    store.db.execute(ins, ("a", now - 3 * DAY, 2.0, 1.0, "objetivo", "completa"))
    store.db.execute(ins, ("b", now - 2 * H, 10.0, 0.0, "ninguna", "pendiente"))   # ventana a medias: no cuenta
    store.db.commit()
    m = A.compute_stats(store.db, now, A._params(None))["medios"]["x.com"]
    assert m["max_favor"] == pytest.approx(2.0) and m["max_contra"] == pytest.approx(1.0)
    assert m["horquilla"] == {"objetivo": 1.0, "stop": 0.0, "ninguna": 0.0} and m["principal"]["casos"] == 2


def test_acertar_a_favor_del_mercado_no_tiene_merito(env):
    store, _ = env
    now = T + 20 * DAY
    A.Aprendizaje(Bus(store), store, bars=FakeBars({}), clock=lambda: now)
    db = store.db
    day0 = (now - 14 * DAY) // DAY
    for k in range(12):   # 12 días en los que casi todo bajó: el 90 % de los movimientos que pasan del umbral, caídas
        d = day0 + k
        db.execute(BASE, (d, 0.05, 0.45, 0.05, 0.45, 0.05, 0.45, 0.05, 0.45))
        for j in range(3):   # bajistas que aciertan a favor del mercado
            db.execute(INS, (f"b{k}-{j}", "BTC", "bajista.com", d * DAY + j * 6 * H, -1, 1.0, -1.0, -1.0))
        for j in range(2):   # alcistas que aciertan contra el mercado
            db.execute(INS, (f"c{k}-{j}", "BTC", "contra.com", d * DAY + j * 6 * H + 3 * H, 1, 1.0, 1.0, 1.0))
    db.commit()
    md = A.compute_stats(db, now, A._params(None))["medios"]
    bj, ct = md["bajista.com"], md["contra.com"]
    assert bj["principal"]["independientes"] == 36 and bj["principal"]["dias"] == 12
    assert bj["principal"]["bruto"] == 1.0 and bj["principal"]["base"] == pytest.approx(0.9)
    assert bj["principal"]["p"] > 0.75 and bj["principal"]["p_aj"] < 0.62 and bj["veredicto"] == "neutro"
    assert bj["peso_auto"] == 1.0
    assert ct["principal"]["base"] == pytest.approx(0.1) and ct["principal"]["p_aj"] > 0.75
    assert ct["veredicto"] == "acierta" and ct["peso_auto"] == pytest.approx(1.5)


def test_fuera_de_tema_no_es_ruido(env):
    store, _ = env
    now = T + 40 * DAY
    A.Aprendizaje(Bus(store), store, bars=FakeBars({}), clock=lambda: now)
    db = store.db
    d = now // DAY - 1
    ins = "INSERT INTO medios_conteo VALUES ('medio', ?, ?, ?, ?, ?, ?, 0)"
    db.execute(ins, ("nhk.or.jp", d, 10, 190, 10, 0))     # generalista: 95 % fuera de tema y lo suyo no se archiva
    db.execute(ins, ("nhk.or.jp", d - 20, 500, 0, 0, 0))  # primer arranque: captadas sin descartes guardados ese día
    db.execute(ins, ("bulos.com", d, 40, 10, 2, 38))      # la IA local archiva el 95 % de lo que pasa el filtro
    db.commit()
    md = A.compute_stats(db, now, A._params(None))["medios"]
    nhk, bulos = md["nhk.or.jp"], md["bulos.com"]
    assert nhk["ruido"]["fuera_de_tema"] == 0.95 and nhk["ruido"]["piezas"] == 200 and nhk["ruido"]["captadas"] == 510
    assert nhk["veredicto"] == "pocos_datos" and "Fuera de tema" in nhk["nota"]
    assert bulos["ruido"]["archivo"] == 0.95 and bulos["veredicto"] == "ruido" and "archiva" in bulos["nota"]


def test_una_base_de_la_primera_version_se_migra(env):
    store, _ = env
    new_cols = (", senal INTEGER, n_medios INTEGER, ts_triaje INTEGER, bolsas TEXT,\n  adelanto_pub INTEGER, acc60 REAL, "
                "acc240 REAL")
    assert new_cols in A.SCHEMA
    old = A.SCHEMA.replace(new_cols, "").replace(", ultima_vez INTEGER, tipo TEXT)", ", ultima_vez INTEGER)")
    assert "senal" not in old and "tipo TEXT)" not in old
    store.db.executescript(old)
    now = T + 10 * DAY
    store.db.execute(INS, ("v1", "BTC", "viejo.com", now - DAY, 1, 1.0, 1.0, 1.0))   # fila vieja: senal NULL
    store.db.commit()
    A.Aprendizaje(Bus(store), store, bars=FakeBars({}), clock=lambda: now)
    cols = {r[1] for r in store.db.execute("PRAGMA table_info(news_outcomes)")}
    assert {"senal", "n_medios", "ts_triaje", "acc240"} <= cols
    md = A.compute_stats(store.db, now, A._params(None))["medios"]
    assert md["viejo.com"]["principal"]["casos"] == 1   # senal NULL y no archivada: cuenta como antes


def test_fijar_peso_rechaza_valores_raros(env):
    store, bus = env
    apr = make(bus, store, FakeBars({}), T)
    for bad in (float("nan"), float("inf"), "abc", True):
        r = apr.fijar_peso("reuters.com", bad)
        assert r["ok"] is False and "número" in r["why"]
    assert apr.fijar_peso("  ", 1.2)["ok"] is False and apr.fijar_peso(None, 1.2)["ok"] is False
    assert store.db.execute("SELECT COUNT(*) FROM medios_peso_hist").fetchone()[0] == 0
    assert A.source_weight("reuters.com") == 1.0
    ok = apr.fijar_peso("reuters.com", "1.2")
    assert ok["ok"] and A.source_weight("reuters.com") == 1.2 and math.isfinite(ok["despues"])


async def test_veredicto_con_histeresis_y_poda(env):
    store, bus = env
    now = T + 500 * DAY
    apr = make(bus, store, FakeBars({}), now)
    db = store.db

    def res(ver):
        m = {**A.summarize({}, apr.p), "medio": "x.com", "peso_auto": 1.0, "veredicto": ver, "nota": "n", "ruido": None,
             "ops": {"n": 0, "R": 0.0, "R_a_favor": 0.0}}
        return {"medios": {"x.com": m}, "alias": {}}

    hist = lambda: db.execute("SELECT tipo, antes, despues FROM medios_peso_hist WHERE outlet = 'x.com'").fetchall()
    for _ in range(A.PARAMS["veredicto_rondas"] - 1):
        r = res("ruido")
        apr._apply_weights(r, now)
        assert r["medios"]["x.com"]["veredicto"] == "pocos_datos"   # se muestra lo confirmado
    assert "x.com" not in apr.weights and hist() == []
    apr._apply_weights(res("ruido"), now)   # se repite: cuenta y se apunta (candidato a quitar)
    assert apr.weights["x.com"]["veredicto"] == "ruido" and hist() == [("veredicto", 1.0, 1.0)]
    apr._apply_weights(res("neutro"), now)   # un vaivén de una ronda no cambia nada
    apr._apply_weights(res("ruido"), now)
    assert apr.weights["x.com"]["veredicto"] == "ruido" and len(hist()) == 1
    # Poda: el historial de pesos y las aportaciones de más de guardar_d días se borran.
    db.execute("INSERT INTO medios_peso_hist(ts, outlet, antes, despues, tipo, motivo, cuerpo) "
               "VALUES (?, 'viejo.com', 1, 1.2, 'auto', 'x', '{}')", (now - 401 * DAY,))
    db.execute("INSERT INTO aportaciones VALUES ('t', 's', 'viejo.com', 1, 1, 1.0, ?)", (now - 401 * DAY,))
    db.commit()
    await apr._prune(now)
    assert db.execute("SELECT COUNT(*) FROM medios_peso_hist WHERE outlet = 'viejo.com'").fetchone() == (0,)
    assert db.execute("SELECT COUNT(*) FROM aportaciones").fetchone() == (0,) and len(hist()) == 1


def test_aportacion_de_una_noticia_sin_direccion_no_suma():
    t = {"side": "long", "R": 2.0, "closed": T}
    news = [{"story": "a", "outlet": "a.com", "dir": 0}, {"story": "b", "outlet": "b.com", "dir": 1},
            {"story": "c", "outlet": "c.com", "dir": -1}]
    assert [a[4] for a in A.contributions("t1", t, news, "historia")] == [0, 1, -1]


def test_base_de_mercado():
    knots = [(T - DAY, 100.0), (T + 2 * DAY, 100.0 * 1.01 ** 72)]   # sube ~1 % por hora, sin parar
    bars = series(knots, T - DAY, T + 2 * DAY)
    dia = T // DAY
    b = A.market_base(bars, dia, T + 2 * DAY, 0.3)
    s15, b15, s60, b60 = b[:4]
    assert s60 == 1.0 and b60 == 0.0 and b15 == 0.0 and b[-2] == 1
    part = A.market_base(bars, dia, dia * DAY + 12 * H, 0.3)   # medio día de velas: aún no está completa
    assert part[-2] == 0 and part[2] == 1.0 and part[6] is None   # sin datos a 24 h todavía
    assert A.market_base(np.empty((0, 5)), dia, T, 0.3)[0] is None


# ---------------------------------------------------------------- operaciones cerradas y post-mortems

VIG_COLS = ("id", "estrategia", "apertura", "moneda", "lado", "entrada_t", "entrada", "stop", "objetivo", "salida_t",
            "salida", "R", "pct", "motivo", "cuerpo")


def add_trades(store):
    db = store.db
    db.execute("CREATE TABLE IF NOT EXISTS trades(id TEXT PRIMARY KEY, story TEXT, opened INTEGER, closed INTEGER, body TEXT)")
    ap = {"id": "t-ap", "story": "a-europa-2025-11-11-btc-1", "strategy": "impulso-largo-noticia-a-favor",
          "window": "Apertura Europa", "market": "londres", "coin": "BTC", "side": "long", "entry": 101.2, "exit": 110.0,
          "opened": EU_OPEN + 30 * MIN, "closed": T + 10 * H, "reason": "Objetivo", "R": 2.9, "pnl_pct": 8.7,
          "headline": "Apertura Europa · Compra BTC", "instrument": "spot",
          "tags": {"impulso_pct": 0.35, "medida_min": 15, "noticia": "a_favor", "noticia_fuerza": 0.4,
                   "apertura_anterior": "a_favor", "btc": "a_favor"}}
    f2 = {"id": "t-n1", "story": "n1", "strategy": "Reacción a noticia", "window": "", "market": None, "coin": "BTC",
          "side": "short", "entry": 100.1, "exit": 101.1, "opened": T + 10 * MIN, "closed": T + 3 * H,
          "reason": "Stop de pérdida", "R": -1.0, "pnl_pct": -1.0, "headline": "La Fed recorta los tipos"}
    abierta = {**f2, "id": "t-open", "closed": None}
    for t in (ap, f2, abierta):
        db.execute("INSERT INTO trades VALUES (?, ?, ?, ?, ?)", (t["id"], t["story"], t["opened"], t["closed"], json.dumps(t)))
    db.execute(f"CREATE TABLE IF NOT EXISTS vigilancia({', '.join(VIG_COLS)})")
    base = {"estrategia": "ap_mom:15:0,2|…", "apertura": "europa-2025-11-11", "moneda": "BTC", "lado": "largo",
            "entrada_t": EU_OPEN + 30 * MIN, "entrada": 101.0, "stop": 98.0, "objetivo": 110.0, "salida_t": T + 9 * H,
            "salida": 110.1, "R": 2.95, "pct": 8.9, "motivo": "objetivo"}
    for vid, extra in (("E2-1", {"previa": False, "ventana_nombre": "Europa"}), ("E2-0", {"previa": True}),
                       ("E2-2", None)):
        r = {**base, "id": vid, "cuerpo": json.dumps(extra or {})}
        if extra is None:
            r.update(salida_t=None, R=None, salida=None)
        db.execute(f"INSERT INTO vigilancia VALUES ({','.join('?' * len(VIG_COLS))})", [r[c] for c in VIG_COLS])
    db.commit()


async def test_postmortems_y_aportaciones(env):
    store, bus = env
    up = series([(T - 3 * DAY, 100.0), (T, 100.0), (T + 60 * MIN, 101.0), (T + 20 * DAY, 101.0)])
    # La cripto confirmó la macro antes de que se abrieran las dos operaciones: entra en su señal (bot/reflejo.py)
    for ev in (captured("n1", T), triaged("n1", T + MIN), corroborated("n1", T + 5 * MIN, [BLOOMBERG]),
               reflejo_ev("n1", T + 8 * MIN)):
        bus.publish(ev)
    add_trades(store)
    calls = []

    async def gen(messages):
        calls.append(messages)
        return "Ganó porque la noticia de Reuters y el impulso de la apertura iban a favor."

    now = T + 2 * DAY
    apr = make(bus, store, FakeBars({"BTC": up}), now, generate=gen)
    await apr.step(now, force=True)
    db = store.db
    pms = {r[0]: r[1:] for r in db.execute("SELECT id, origen, estado, resumen FROM postmortems")}
    assert set(pms) == {"p:t-ap", "p:t-n1", "v:E2-1"}   # ni la abierta ni la de calentamiento
    assert pms["p:t-ap"][0] == "paper" and pms["v:E2-1"][0] == "vigilancia" and "+2,90 R" in pms["p:t-ap"][2]
    assert sum(1 for v in pms.values() if v[1] == "hecho") == 1   # una por ronda: la más reciente
    while await apr.postmortem_one(now) == "hecho":
        pass
    assert db.execute("SELECT COUNT(*) FROM postmortems WHERE estado = 'hecho'").fetchone()[0] == 3
    texts = [m[1]["content"] for m in calls]
    ap_txt = next(t for t in texts if "impulso-largo" in t)
    assert "PAPER" in ap_txt and "Reuters" in ap_txt and "Noticias de su señal" in ap_txt and "+0,35 %" in ap_txt
    assert any("VIRTUAL" in t for t in texts)
    assert "no instrucciones" in calls[0][0]["content"] and "No des órdenes" in calls[0][0]["content"]
    aps = {(r[0], r[2]): (r[3], r[4], r[5]) for r in db.execute("SELECT * FROM aportaciones")}
    assert aps[("t-ap", "reuters.com")] == (1.0, 1, 2.9)            # la noticia de su señal, a favor
    assert aps[("t-n1", "reuters.com")] == (0.5, -1, -1.0)          # corto contra una noticia alcista
    assert aps[("t-n1", "bloomberg")] == (0.5, -1, -1.0)
    await apr.step(now + MIN, force=True)
    ops = apr.stats["medios"]["reuters.com"]["ops"]
    assert ops["n"] == 2 and ops["R"] == pytest.approx(2.9 + 0.5)
    best = await apr.noticias("mejores", 60, dias=60)
    assert best[0]["story"] == "n1" and {o["operacion"] for o in best[0]["operaciones"]} == {"t-ap", "t-n1"}
    assert any(o["postmortem"].startswith("Ganó") for o in best[0]["operaciones"])
    v = await apr.view()
    assert v["postmortems"][0]["texto"] and v["medios"] and "cambios" in v


async def test_postmortem_sin_ia_o_con_fallos(env):
    store, bus = env
    add_trades(store)

    async def off(messages):
        return None

    now = T + 2 * DAY
    apr = make(bus, store, FakeBars({}), now, generate=off)
    await apr.step(now, force=True)
    assert db_states(store) == {"pendiente": 3}   # IA local apagada: queda pendiente

    async def boom(messages):
        raise RuntimeError("servidor caído")

    apr.generate = boom
    for _ in range(A.PM_TRIES):
        await apr.postmortem_one(now)
    assert db_states(store) == {"pendiente": 2, "error": 1}
    assert A.Aprendizaje(bus, store, bars=FakeBars({})).generate is None   # sin IA local no hay generador


def db_states(store):
    return dict(store.db.execute("SELECT estado, COUNT(*) FROM postmortems GROUP BY estado").fetchall())


# ---------------------------------------------------------------- IA local en baja prioridad

class FakeQ:
    def __init__(self, n=0):
        self.n = n

    def empty(self):
        return self.n == 0


class FakeResp:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    async def json(self):
        return {"choices": [{"message": {"content": " Explicación corta. "}}]}


class FakeSession:
    def __init__(self):
        self.posts = []

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        return FakeResp()


class FakeAI:
    def __init__(self, ready=True, queued=0):
        self.ready, self.q, self.acq, self.s, self.last_use = ready, FakeQ(queued), 0, FakeSession(), 0.0
        self.base = "http://127.0.0.1:8094"

    async def acquire(self):
        self.acq += 1
        return True


async def test_generador_local_baja_prioridad():
    msgs = [{"role": "user", "content": "hechos"}]
    off = FakeAI(ready=False)
    assert await A.local_generator(off)(msgs) is None and off.acq == 0   # no la enciende para esto
    # Con la cola de triaje llena (n.º 15 y 4): un post-mortem cada 10 min como mucho pasa por delante (hueco reservado)
    busy, reloj = FakeAI(queued=120), [1000.0]
    gen = A.local_generator(busy, clock=lambda: reloj[0])
    assert await gen(msgs) == "Explicación corta." and busy.acq == 1
    reloj[0] += A.PM_RESERVA_S - 1
    assert await gen(msgs) is None and busy.acq == 1   # ya tuvo su hueco: primero el triaje
    reloj[0] += 2
    assert await gen(msgs) == "Explicación corta." and busy.acq == 2
    ai = FakeAI()
    assert await A.local_generator(ai)(msgs) == "Explicación corta."
    url, body = ai.s.posts[0]
    assert url.endswith("/v1/chat/completions") and body["model"] == config.LOCAL_AI_ALIAS and body["messages"] == msgs


# ---------------------------------------------------------------- mensaje del bus y fallos

async def test_mensaje_y_fallos_que_no_paran(env):
    store, bus = env
    up = series([(T - 3 * DAY, 100.0), (T, 100.0), (T + 60 * MIN, 101.0), (T + 20 * DAY, 101.0)])
    for ev in (captured("n1", T), triaged("n1", T + MIN)):
        bus.publish(ev)
    sent = []
    orig = bus.publish
    bus.publish = lambda msg: (sent.append(msg) if msg.get("type") == "aprendizaje" else None) or orig(msg)
    bars = FakeBars({"BTC": up})
    now = T + 2 * DAY
    apr = make(bus, store, bars, now)
    await apr.step(now, force=True)
    assert len(sent) == 1
    msg = bus.state["aprendizaje"]
    assert msg["estado"] == "ok" and "poca muestra" in msg["nota"] and "Solo paper" in msg["aviso"]
    for k in ("totales", "global", "medios_mejores", "medios_peores", "medios_ruido", "medios_nuevos", "por_nivel",
              "por_region", "por_idioma", "por_tipo", "por_ventana", "por_fase", "por_rol", "por_moneda", "datos_hasta",
              "medios_candidatos"):
        assert k in msg
    assert msg["totales"]["filas"] == 1 and msg["global"]["casos"] == 1
    await apr.step(now + MIN)            # sin cambios: no se vuelve a publicar
    assert len(sent) == 1
    bars.fail = RuntimeError("disco roto")
    await apr.step(now + 2 * MIN, force=True)   # un fallo de datos no tumba la ronda: se ve en el estado
    assert "medida" in apr.errors and bus.state["aprendizaje"]["estado"] == "error"
    assert "disco roto" in bus.state["aprendizaje"]["nota"]
    bars.fail = None
    await apr.step(now + 3 * MIN, force=True)
    assert not apr.errors and bus.state["aprendizaje"]["estado"] == "ok"
    stale = make(bus, store, FakeBars({"BTC": up}, cap=T + 3 * H), T + 2 * DAY)   # velas que no llegan
    await stale.step(T + 2 * DAY, force=True)
    assert bus.state["aprendizaje"]["estado"] == "datos_atrasados"


def test_explicacion_de_una_noticia():
    r = {"coin": "BTC", "dir": 1, "direccion": 60, "magnitud": 50, "importancia": 7, "r60": 0.8, "r240": 1.6,
         "previo": 0.9, "max_sube": 2.1, "max_baja": 0.7, "rol": "primera", "adelanto": 12 * MIN, "nombre": "Reuters",
         "verificada": 0, "verif": 0.3, "fase": "previa", "ventana": "europa", "min_apertura": -120, "ap240": 0.5}
    t = A.explain_news(r)
    assert t.startswith("Prevista alcista para BTC") and "acertó" in t and "descontada" in t and "12 min antes" in t
    assert "puso en duda" in t and "apertura «europa»" in t and "+0,50 %" in t
    assert "falló" in A.explain_news({**r, "r240": -0.9}) and "ruido" in A.explain_news({**r, "r240": 0.1})
    assert "primera que captó el bot" in t and "latencia de los feeds" in t and "en 24 h" in t
    pend = A.explain_news({**r, "estado": "pendiente", "medido": 240})   # ventana de 24 h aún abierta
    assert "hasta ahora (+4 h de 24)" in pend and "en 24 h" not in pend
    tri = A.explain_news({**r, "t0": T, "ts_triaje": T + 30 * MIN, "acc240": 0.4})
    assert "trió 30 min después" in tri and "+0,40 %" in tri
