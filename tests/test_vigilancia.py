"""Vigilancia en vivo (bot/vigilancia.py) con velas sintéticas y sin red: resolución de las operaciones virtuales con
los costes y el funding del laboratorio, la variante del usuario de 7 días, sin duplicados tras un reinicio (tampoco
sin caché y con Binance caído), una posición por moneda entre rondas, velas a medias, mensaje «vigilancia», ruta
/api/vigilancia y fallos de datos que no paran nada ni llenan el registro."""
import asyncio
import json
import logging
from datetime import date

import numpy as np
import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot import apertura, config
from bot import laboratorio as L
from bot import vigilancia as V
from bot.bus import Bus
from bot.server import Services, make_app
from bot.store import Store

MIN, H, DAY = L.MIN, L.H, L.DAY
SID = "ap_mom:15:0,2|ninguna|todos|ninguno|todas|todas|todos|t:30|3|3|7d"
NY = lambda d: next(o["open_ms"] for o in apertura.openings_on(d) if o["id"] == "nuevayork")
O1, O2 = NY(date(2025, 11, 10)), NY(date(2025, 11, 11))
DESDE = V.next_midnight("2025-11-09")          # medianoche de Madrid del 10-11-2025
T0, T1 = DESDE - 72 * DAY, O2 + 3 * DAY
FUND = 0.0001


def series(knots):
    """Velas de 1 min [t, o, h, l, c, v, qv, taker] que siguen la línea quebrada knots [(t, precio), ...]."""
    t = np.arange(T0, T1, MIN, dtype=np.int64)
    c = np.interp(t.astype(float), [k[0] for k in knots], [k[1] for k in knots])
    o = np.r_[c[0], c[:-1]]
    v = np.full(len(t), 10.0)
    return np.column_stack([t, o, np.maximum(o, c), np.minimum(o, c), c, v, v * c, v / 2])


def scenario_a():
    """BTC: impulso +0,5 % y objetivo (+9 %) a las 5 h; ETH: impulso y stop (−3 %); SOL: impulso −0,5 %, corto que
    cobra su objetivo; XRP: plano (sin señal)."""
    return {"BTC": series([(T0, 100), (O1, 100), (O1 + 15 * MIN, 100.5), (O1 + 270 * MIN, 100.5), (O1 + 300 * MIN, 110)]),
            "ETH": series([(T0, 100), (O1, 100), (O1 + 15 * MIN, 100.5), (O1 + 120 * MIN, 100.5), (O1 + 130 * MIN, 96)]),
            "SOL": series([(T0, 100), (O1, 100), (O1 + 15 * MIN, 99.5), (O1 + 120 * MIN, 99.5), (O1 + 150 * MIN, 89)]),
            "XRP": series([(T0, 100), (T1, 100)])}


class Feed:
    """API de klines falsa: velas cerradas desde start (como apertura.to_bars), de limit en limit."""

    def __init__(self, bars, now):
        self.bars, self.now, self.calls, self.fail = bars, now, 0, None

    async def __call__(self, coin, start, limit):
        self.calls += 1
        if self.fail:
            raise self.fail
        a = self.bars[coin]
        i = int(np.searchsorted(a[:, 0], start))
        j = int(np.searchsorted(a[:, 0], self.now[0] - MIN, side="right"))
        return [[int(r[0]), *map(float, r[1:])] for r in a[i:min(j, i + limit)]]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HIST_DIR", tmp_path / "hist")
    monkeypatch.setattr(config, "BACKTEST_DIR", tmp_path / "bt")
    (tmp_path / "hist" / "funding").mkdir(parents=True)
    ft = np.arange(T0 // (8 * H) * 8 * H, T1 + 2 * DAY, 8 * H)
    (tmp_path / "hist" / "funding" / "SOLUSDT.json").write_text(json.dumps([[int(t), FUND] for t in ft]))
    return tmp_path


def item(sid=SID, code="E1"):
    it = V.watch_item(sid, DESDE, "Impulso de 15 min a +30 min", ref={"oos": {"trades": 278, "mean_R": 0.1402,
                                                                              "win_rate": 0.518}},
                      period={"split": "2025-08-16", "to": "2026-10-23"})
    it["codigo"] = code
    return it


def make(env, bars, at, name="v.db", watch=None):
    now = [at]
    store = Store(env / name)
    bus = Bus(store)
    feed = Feed(bars, now)
    v = V.Vigilancia(bus, store, clock=lambda: now[0], fetch=feed, watch=watch or [item()],
                     meta={"congelada": "2025-11-09", "lista": "prueba.json"})
    return v, bus, store, feed, now


def by_coin(v):
    return {r["moneda"]: r for r in v.rows.values()}


def net(d, ratio):
    return (d * (ratio - 1) - L.FEE * (1 + ratio)) * 100


def test_estrategia_desde_su_identificador():
    st = V.parse_id(SID)
    assert st["src"] == ("ap_mom", 15, 0.2) and st["entry"] == ("t", 30) and st["family"] == "momento_apertura"
    for sid in ("btc:ma20|rvol:1,5|todos|ninguno|asia|SOL|todos|pb:15|3|2|sig",
                "ap_mom:15:0,2|rvol:1,5|todos|a_favor|todas|todas|todos|t:30|3|3|sig"):
        assert L.skey(V.parse_id(sid)) == sid
    assert V.parse_id(SID.replace("|3|3|7d", "|1|3|7d")) is None        # stop < 1,5 %: fuera de las reglas
    assert V.parse_id(SID.replace("|3|3|7d", "|3|1,5|7d")) is None      # menos de 2R
    # Objetivo de al menos un 3 % neto de comisión y deslizamiento de ida y vuelta (0,24 %): 1,5 % × 2R no llega.
    assert V.parse_id(SID.replace("|3|3|7d", "|1,5|2|7d")) is None
    assert V.net_target_pct(V.parse_id(SID.replace("|3|3|7d", "|1,5|2,5|7d"))) == pytest.approx(3.51)
    assert V.parse_id(SID.replace("7d", "1h")) is None and V.parse_id("roto") is None


def test_lista_congelada_y_prometedoras(env):
    (env / "bt").mkdir()
    frozen = {"date": "2026-10-26", "robust": [], "promising": [{"id": SID, "name": "Congelada", "class": "prometedora"}]}
    other = "btc:ma20|rvol:1,5|todos|ninguno|asia|SOL|todos|pb:15|3|2|sig"
    lab = {"period": {"split": "2025-08-16", "to": "2026-10-23"}, "frozen": {"date": "2026-11-02"},
           "promising": [{"id": SID, "name": "Otra vez", "class": "prometedora", "oos": {"trades": 278, "mean_R": 0.14}},
                         {"id": other, "name": "Nueva", "class": "prometedora", "oos": {"trades": 34, "mean_R": 0.2}},
                         {"id": "roto|x", "name": "Mal"}]}
    (env / "bt" / V.FROZEN).write_text(json.dumps(frozen), encoding="utf-8")
    (env / "bt" / V.LAB_FILE).write_text(json.dumps(lab), encoding="utf-8")
    items, meta = V.load_watch()
    other_7d = other.replace("|sig", "|7d")                            # su variante del usuario, aparte
    assert [(i["codigo"], i["id"], i["origen"]) for i in items] == [("E1", SID, "congelada"), ("E2", other, "laboratorio"),
                                                                    ("E2-7d", other_7d, "variante")]
    assert items[2]["variante_de"] == other and items[2]["desde"] == items[1]["desde"] and items[2]["bt"] is None
    assert items[0]["desde"] == V.next_midnight("2026-10-26") and items[1]["desde"] == V.next_midnight("2026-11-02")
    assert items[0]["nombre"] == "Congelada" and items[0]["bt"]["operaciones"] == 278
    assert items[0]["bt"]["periodo"] == "2025-08-16 a 2026-10-23" and meta["congelada"] == "2026-10-26"


async def test_resuelve_las_operaciones_virtuales_con_costes_y_funding(env):
    v, bus, store, feed, now = make(env, scenario_a(), O1 + 360 * MIN)
    await v.step()
    rows = by_coin(v)
    assert set(rows) == {"BTC", "ETH", "SOL"}                       # XRP plano: sin señal
    b, e, s = rows["BTC"], rows["ETH"], rows["SOL"]
    assert (b["lado"], b["motivo"], e["motivo"], s["lado"], s["motivo"]) == ("largo", "objetivo", "stop", "corto", "objetivo")
    for r in (b, e, s):
        assert r["entrada_t"] == O1 + 30 * MIN and r["apertura"] == "nuevayork-2025-11-10"
        assert not r["cuerpo"]["previa"]
    E = 100.5 * (1 + L.SLIP)
    assert b["entrada"] == pytest.approx(E) and b["stop"] == pytest.approx(E * 0.97) and b["objetivo"] == pytest.approx(E * 1.09)
    assert b["R"] == pytest.approx(net(1, 1.09 * (1 - L.SLIP)) / 3, abs=1e-3)
    assert e["R"] == pytest.approx(net(1, 0.97 * (1 - L.SLIP - L.STOP_EXTRA)) / 3, abs=1e-3)
    assert -1.1 < e["R"] < -1.05 and 2.8 < b["R"] < 3.0             # ETH lleva 5 pb extra en el stop
    # Corto en perpetuo: comisión de perpetuo y el funding liquidado mientras está abierto (lo cobra el corto).
    n_f = sum(1 for t in range(T0 // (8 * H) * 8 * H, T1, 8 * H) if s["entrada_t"] < t <= s["salida_t"])
    assert n_f == 1
    model = net(-1, 0.91 * (1 + L.SLIP))
    assert s["cuerpo"]["pct_modelo"] == pytest.approx(model, abs=1e-3)
    assert s["pct"] == pytest.approx(model + (L.FEE - L.FEE_PERP) * 200 + n_f * FUND * 100, abs=1e-3)
    assert s["R"] == pytest.approx(s["pct"] / 3, abs=1e-4) and s["cuerpo"]["instrumento"] == "perpetuo"
    assert s["cuerpo"]["funding_completo"] is True
    assert b["salida"] == pytest.approx(E * 1.09 * (1 - L.SLIP), rel=1e-6)
    # Guardadas en la tabla propia
    db = store.db.execute("SELECT moneda, motivo, R FROM vigilancia ORDER BY moneda").fetchall()
    assert [(m, w) for m, w, _ in db] == [("BTC", "objetivo"), ("ETH", "stop"), ("SOL", "objetivo")]


async def test_abierta_y_luego_resuelta_sin_duplicados_tras_reiniciar(env):
    bars = scenario_a()
    v, bus, store, feed, now = make(env, bars, O1 + 250 * MIN)
    await v.step()
    b = by_coin(v)["BTC"]
    assert b["salida_t"] is None and b["R"] is None and b["motivo"] == "abierta"
    assert b["cuerpo"]["R_latente"] == pytest.approx(net(1, 100.5 / (100.5 * (1 + L.SLIP)) * (1 - L.SLIP)) / 3, abs=1e-3)
    st = v.msg["estrategias"][0]
    assert st["vivo"]["abiertas"] == 1 and st["vivo"]["operaciones"] == 2 and st["abiertas"][0]["moneda"] == "BTC"
    now[0] = O1 + 360 * MIN
    await v.step()
    assert by_coin(v)["BTC"]["motivo"] == "objetivo"
    before = {r["id"]: dict(r) for r in v.rows.values()}
    await v.close()                                                  # guarda las velas nuevas en la caché
    assert (env / "hist" / "1m" / "BTCUSDT-1m-2025-11-api.json").exists()
    store.close()
    # Reinicio: misma base; las velas salen de la caché y solo se pide lo que falta.
    v2, bus2, store2, feed2, now2 = make(env, bars, O2 + 360 * MIN)
    assert {k: r["salida_t"] for k, r in v2.rows.items()} == {k: r["salida_t"] for k, r in before.items()}
    await v2.step()
    assert feed2.calls <= 3 * len(L.COINS)
    assert len(v2.rows) == 3 and store2.db.execute("SELECT COUNT(*) FROM vigilancia").fetchone()[0] == 3
    for k, r in v2.rows.items():
        assert r == before[k]
    assert v2.msg["aperturas_evaluadas"] == 10                       # las 5 ventanas de dos días


async def test_una_posicion_por_moneda_entre_rondas(env, monkeypatch):
    """La operación de una apertura que ya no se recalcula sigue ocupando su moneda: la de la apertura siguiente que
    habría entrado mientras tanto no aparece al mover la ventana."""
    flat = series([(T0, 100), (T1, 100)])
    bars = {"BTC": series([(T0, 100), (O1, 100), (O1 + 15 * MIN, 100.5), (O2, 100.5), (O2 + 15 * MIN, 101),
                           (O2 + 120 * MIN, 101), (O2 + 130 * MIN, 97)]), "ETH": flat, "SOL": flat, "XRP": flat}
    v, bus, store, feed, now = make(env, bars, O2 + 300 * MIN)
    await v.step()
    [a] = v.rows.values()
    assert a["apertura"] == "nuevayork-2025-11-10" and a["motivo"] == "stop" and a["salida_t"] > O2 + 30 * MIN
    monkeypatch.setattr(V, "KEEP_D", 0.5)                            # la apertura del día 10 queda fuera del cálculo
    now[0] = O2 + 360 * MIN
    v._need = True
    await v.step()
    assert list(v.rows) == [a["id"]]
    # Sin la operación guardada ocupando BTC, la del día 11 sí entraría: la prueba mide algo.
    monkeypatch.setattr(V.Vigilancia, "_blockers", lambda self, ev_from: {})
    v._need = True
    await v.step()
    assert sorted(r["apertura"] for r in v.rows.values()) == ["nuevayork-2025-11-10", "nuevayork-2025-11-11"]


async def test_mensaje_solo_si_cambia_y_ruta(env):
    v, bus, store, feed, now = make(env, scenario_a(), O1 + 360 * MIN)
    q = bus.subscribe()
    await v.step()
    msgs = [json.loads(q.get_nowait()) for _ in range(q.qsize())]
    [m] = [x for x in msgs if x["type"] == "vigilancia"]
    assert bus.state["vigilancia"] == m and m["estado"] == "ok" and "VIRTUALES" in m["aviso"]
    s = m["estrategias"][0]
    assert s["codigo"] == "E1" and s["vivo"]["operaciones"] == 3 and s["vivo"]["abiertas"] == 0
    assert s["vivo"]["ganadoras"] == pytest.approx(2 / 3, abs=1e-4) and s["vivo"]["largos"] == 2
    assert s["vivo"]["por_motivo"] == {"objetivo": 2, "stop": 1, "tiempo": 0}
    assert s["backtest"] == {"operaciones": 278, "R_medio": 0.1402, "ganadoras": 0.518, "pct_medio": None,
                             "largos": None, "R_medio_cortos_perp": None, "periodo": "2025-08-16 a 2026-10-23"}
    assert m["aperturas_evaluadas"] == 5 and m["aperturas"][0]["key"] == "nuevayork-2025-11-10"
    assert len(m["aperturas"][0]["entradas"]) == 3 and m["desde_dia"] == "2025-11-10"
    assert m["proxima"]["key"] == "tokio-seul-2025-11-11"
    await v.step()                                                   # nada nuevo: no se vuelve a publicar
    assert q.empty()
    async with TestClient(TestServer(make_app(bus, lambda: {}, Services(vigilancia=v)))) as client:
        r = await client.get("/api/vigilancia")
        assert r.status == 200
        j = await r.json()
        assert j["type"] == "vigilancia" and len(j["operaciones"]) == 3 and j["estrategias"][0]["id"] == SID
    async with TestClient(TestServer(make_app(bus, lambda: {}))) as client:   # sin el módulo: el último estado
        j = await (await client.get("/api/vigilancia")).json()
        assert j["type"] == "vigilancia" and j["estrategias"][0]["codigo"] == "E1"


async def test_fallos_de_datos_y_de_calculo_no_paran_nada(env, monkeypatch):
    v, bus, store, feed, now = make(env, scenario_a(), O1 + 360 * MIN)
    feed.fail = RuntimeError("Binance caído")
    await v.step()
    assert bus.state["vigilancia"]["estado"] == "sin_datos" and "Binance caído" in bus.state["vigilancia"]["nota"]
    feed.fail = None
    monkeypatch.setattr(V, "evaluate", lambda *a: 1 / 0)
    await v.step()
    assert bus.state["vigilancia"]["estado"] == "error" and not v.rows
    monkeypatch.undo()
    monkeypatch.setattr(config, "HIST_DIR", env / "hist")
    monkeypatch.setattr(config, "BACKTEST_DIR", env / "bt")
    await v.step()                                                   # vuelve solo en la ronda siguiente
    assert bus.state["vigilancia"]["estado"] == "ok" and len(v.rows) == 3


def test_variante_del_usuario_que_opera_el_paper(env):
    """La congelada de «sig» sigue como prueba de la hipótesis; su variante de 7 días va aparte, marcada como la que
    opera el paper, con la cifra de la réplica aparte del laboratorio (no congelada)."""
    (env / "bt").mkdir()
    ref = apertura.LAB_REF
    frozen = {"date": "2026-10-26", "promising": [{"id": ref, "name": "Congelada", "class": "prometedora"}]}
    (env / "bt" / V.FROZEN).write_text(json.dumps(frozen), encoding="utf-8")
    a, b = V.load_watch()[0]
    assert (a["codigo"], a["origen"], a["st"]["dur"], a["en_paper"], a["bt"]) == ("E1", "congelada", "sig", False, None)
    assert (b["codigo"], b["origen"], b["clase"], b["variante_de"]) == ("E1-7d", "variante", "variante", ref)
    assert b["id"] == ref.replace("|sig", "|7d") and b["en_paper"] and b["desde"] == a["desde"]
    assert b["reglas"]["dur_texto"] == L.DUR_TEXT["7d"] and b["nombre"].endswith(L.DUR_TEXT["7d"])
    assert b["bt"]["operaciones"] == 202 and b["bt"]["R_medio"] == 0.3459 and "no congelada" in b["bt"]["fuente"]


async def test_la_variante_de_7_dias_sigue_cuando_la_congelada_cierra_por_tiempo(env):
    flat = series([(T0, 100), (T1, 100)])
    bars = {"BTC": series([(T0, 100), (O1, 100), (O1 + 15 * MIN, 100.5), (O2 + 60 * MIN, 100.5), (O2 + 90 * MIN, 110)]),
            "ETH": flat, "SOL": flat, "XRP": flat}
    watch = [item(SID.replace("|7d", "|sig"), "E1"), item(SID, "E1-7d")]
    v, bus, store, feed, now = make(env, bars, O2 + 360 * MIN, watch=watch)
    await v.step()
    rows = {r["estrategia"]: r for r in v.rows.values()}
    sig, d7 = rows[watch[0]["id"]], rows[SID]
    assert sig["entrada_t"] == d7["entrada_t"] == O1 + 30 * MIN and sig["moneda"] == d7["moneda"] == "BTC"
    assert sig["motivo"] == "tiempo" and abs(sig["salida_t"] - O2) <= 2 * MIN and sig["R"] < 0
    assert d7["motivo"] == "objetivo" and d7["salida_t"] > O2 + 60 * MIN and 2.8 < d7["R"] < 3.0
    assert [s["codigo"] for s in v.msg["estrategias"]] == ["E1", "E1-7d"]


async def test_reinicio_sin_cache_y_con_binance_caido_no_anula_las_abiertas(env, caplog):
    bars = scenario_a()
    v, bus, store, feed, now = make(env, bars, O1 + 250 * MIN)
    await v.step()
    b = by_coin(v)["BTC"]
    assert b["motivo"] == "abierta"
    ids = set(v.rows)
    store.close()                                                    # sin guardar velas: la caché queda vacía
    assert not (env / "hist" / "1m").exists()
    v2, bus2, store2, feed2, now2 = make(env, bars, O1 + 260 * MIN)
    count = lambda: store2.db.execute("SELECT COUNT(*) FROM vigilancia").fetchone()[0]
    with caplog.at_level(logging.INFO, logger="vigilancia"):
        feed2.fail = RuntimeError("Binance caído")
        await v2.step()
        assert set(v2.rows) == ids and count() == len(ids)
        m = bus2.state["vigilancia"]
        assert m["estado"] == "sin_datos" and m["estrategias"][0]["vivo"]["abiertas"] == 1
        feed2.fail = None                                            # una moneda sin velas: tampoco se calcula
        feed2.bars = {**bars, "XRP": bars["XRP"][:0]}
        await v2.step()
        assert set(v2.rows) == ids and count() == len(ids)
        feed2.bars = bars                                            # vuelven las 4: la misma operación, sin duplicar
        await v2.step()
    assert set(v2.rows) == ids and v2.rows[b["id"]]["motivo"] == "abierta" and count() == len(ids)
    assert not [r for r in caplog.records if "anulada" in r.getMessage() or "entra" in r.getMessage()]
    # La regla de anular: solo con la estrategia calculada, la apertura y la moneda evaluadas y velas que lleguen hasta
    # donde llegaba la operación.
    op = {"key": b["apertura"], "open_ms": O1, "monedas": list(L.COINS)}
    full = {"trades": {SID: []}, "openings": [op], "calc": [SID], "hasta": {c: O1 + 400 * MIN for c in L.COINS}}
    for res in ({**full, "calc": []}, {**full, "openings": [{**op, "monedas": ["ETH"]}]},
                {**full, "hasta": {"BTC": O1 + 200 * MIN}}):
        v2._save(res, O1 - DAY, now2[0], v2.watch)
        assert b["id"] in v2.rows
    v2._save(full, O1 - DAY, now2[0], v2.watch)
    assert b["id"] not in v2.rows and count() == len(ids) - 1


class _Resp:
    def __init__(self, data):
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    async def json(self, content_type=None):
        return self.data


class _Session:
    def __init__(self, data):
        self.data = data

    def get(self, url, params=None, timeout=None):
        return _Resp(self.data)


async def test_velas_a_medias_no_pasan_por_cerradas(env):
    """Con el reloj del PC adelantado, la vela en curso parece cerrada: hasta SETTLE_MS después no cuenta."""
    t = 29_000_000 * MIN
    k = lambda t0, c: [t0, "1", "2", "0.5", str(c), "10", t0 + MIN - 1, "10", 5, "4", "4", "0"]
    clock = [t + MIN + 500]
    store = Store(env / "v.db")
    v = V.Vigilancia(Bus(store), store, session=_Session([k(t - MIN, 1.0), k(t, 1.5)]), clock=lambda: clock[0],
                     watch=[item()])
    assert [r[0] for r in await v._fetch_binance("BTC", t - MIN, 1000)] == [t - MIN]
    clock[0] = t + MIN + V.SETTLE_MS + 1
    assert [r[0] for r in await v._fetch_binance("BTC", t - MIN, 1000)] == [t - MIN, t]


async def test_se_repiden_los_ultimos_minutos_y_a_la_cache_solo_las_asentadas(env):
    bars = scenario_a()
    at = O1 + 360 * MIN
    v, bus, store, feed, now = make(env, bars, at)
    await v.step()
    b2 = bars["BTC"].copy()                                          # la última vela se había tomado a medias
    i = int(np.searchsorted(b2[:, 0], at - MIN))
    b2[i, 4] = 123.0
    feed.bars = {**bars, "BTC": b2}
    now[0] = at + MIN
    await v.step()
    a = v.bars["BTC"]
    assert a[np.searchsorted(a[:, 0], at - MIN), 4] == 123.0 and a[-1, 0] == at
    await v.close()
    rows = json.loads((env / "hist" / "1m" / "BTCUSDT-1m-2025-11-api.json").read_text())["rows"]
    assert rows[-1][0] == at - 2 * MIN                               # las de los 2 últimos minutos, aún no
    assert {int(r[0]) for r in v.unsaved["BTC"]} == {at - MIN, at}


async def test_un_fallo_de_calculo_repetido_no_llena_el_registro(env, monkeypatch, caplog):
    v, bus, store, feed, now = make(env, scenario_a(), O1 + 360 * MIN)
    monkeypatch.setattr(V, "evaluate", lambda *a: 1 / 0)
    with caplog.at_level(logging.WARNING, logger="vigilancia"):
        for _ in range(4):
            now[0] += MIN
            await v.step()
    assert len([r for r in caplog.records if r.exc_info]) == 1     # la traza completa, una vez cada ERR_LOG_S
    assert v.calc_err and bus.state["vigilancia"]["estado"] == "error"


async def test_estado_apagada_y_arrancando(env, monkeypatch):
    store = Store(env / "v.db")
    bus = Bus(store)
    v = V.Vigilancia(bus, store, clock=lambda: O1)
    assert v.view()["estado"] == "arrancando"                        # aún sin leer la lista
    w = make(env, scenario_a(), O1, name="w.db")[0]
    assert w.view()["estado"] == "arrancando"                        # con la lista, antes de la primera ronda
    await v.step()                                                   # no hay lista congelada ni laboratorio.json
    assert v.view()["estado"] == "sin_estrategias"
    monkeypatch.setattr(V, "ENABLED", False)
    await v.run()
    assert bus.state["vigilancia"]["estado"] == "apagada" and v.view()["estado"] == "apagada"
    assert "BOT_VIGILANCIA=0" in v.view()["nota"]


async def test_tras_un_apagon_largo_la_abierta_queda_sin_datos_y_no_bloquea(env):
    v, bus, store, feed, now = make(env, scenario_a(), O1 + 250 * MIN)
    await v.step()
    b = by_coin(v)["BTC"]
    assert b["motivo"] == "abierta" and v._blockers(O1 + DAY) == {SID: [("BTC", b["entrada_t"], V.BIG)]}
    now[0] = O1 + (V.MAX_BACK_D + 10) * DAY
    await v.step()
    r = v.rows[b["id"]]
    assert r["motivo"] == "sin_datos" and r["salida_t"] is None and v._blockers(O1 + DAY) == {}
    assert store.db.execute("SELECT motivo FROM vigilancia WHERE id=?", (b["id"],)).fetchone()[0] == "sin_datos"
    vivo = v.msg["estrategias"][0]["vivo"]
    assert (vivo["operaciones"], vivo["abiertas"], vivo["sin_datos"]) == (2, 0, 1)


async def test_el_funding_va_aparte_y_no_bloquea_la_ronda(env, monkeypatch):
    started = asyncio.Event()

    async def hang(session, sym):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(V.historia_extra, "fetch_funding", hang)
    monkeypatch.setattr(V, "FUNDING_WAIT_S", 3600)
    v, bus, store, feed, now = make(env, scenario_a(), O1 + 360 * MIN)
    v.session = object()
    await asyncio.wait_for(v.step(), 60)
    await asyncio.wait_for(started.wait(), 5)
    assert bus.state["vigilancia"]["estado"] == "ok" and not v._fund_task.done()
    await v.close()
    assert v._fund_task.done()


async def test_aperturas_solo_se_reescriben_si_cambian(env):
    v, bus, store, feed, now = make(env, scenario_a(), O1 + 360 * MIN)
    await v.step()
    keys, orig = [], v._set_state
    v._set_state = lambda k, val: (keys.append(k), orig(k, val))
    now[0] += MIN
    v._need = True
    await v.step()
    assert "ultimo_calculo" in keys and "aperturas" not in keys and "vigiladas" not in keys


def test_seis_diarias_no_rompe_la_vigilancia(monkeypatch):
    """«6 diarias» no es una estrategia del laboratorio (umbral 0, tope de 6 al día y veto de noticias no están en su
    rejilla): no se vigila, y con un preset sin lab ni 7 días el paper no reproduce ninguna vigilada."""
    seis = "ap_mom:15:0|ninguna|todos|ninguno|todas|todas|todos|t:15|1,5|3|24h"
    assert V.parse_id(seis) is None
    assert V.paper_id() == "ap_mom:15:0,2|rvol:1,5|todos|a_favor|todas|todas|todos|t:30|3|3|7d"
    monkeypatch.setattr(apertura, "IMPULSO", dict(apertura.SEIS))
    assert V.paper_id() is None
    it = V.watch_item("ap_mom:15:0,2|rvol:1,5|todos|a_favor|todas|todas|todos|t:30|3|3|7d", 0)
    assert it is not None and it["en_paper"] is False


def test_con_seis_activa_ninguna_vigilada_es_la_del_paper(tmp_path, monkeypatch):
    """Con «6 diarias» en estrategia_activa.json, el paper ya no opera E2: ninguna vigilada lleva en_paper."""
    import importlib.util
    from bot import evolucion
    spec = importlib.util.spec_from_file_location("activar_estrategia", "scripts/activar_estrategia.py")
    act = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(act)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    e2 = "ap_mom:15:0,2|rvol:1,5|todos|a_favor|todas|todas|todos|t:30|3|3|7d"
    assert V.paper_es_impulso() and V.paper_id() == e2   # sin archivo: el preset de siempre
    evolucion.escribir_activa(tmp_path / evolucion.ARCHIVO, act.construir("seis", 1793180000000, "v-prueba"))
    assert not V.paper_es_impulso() and V.paper_id() is None
    assert V.watch_item(e2, 0)["en_paper"] is False
    evolucion.escribir_activa(tmp_path / evolucion.ARCHIVO, act.construir("impulso", 1793180000000, "v-vuelta"))
    assert V.paper_es_impulso() and V.paper_id() == e2
