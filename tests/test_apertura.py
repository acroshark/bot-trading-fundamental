import math
from datetime import date, datetime, timezone

import pytest

from bot import apertura as ap
from bot.apertura import MIN

T0 = int(datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc).timestamp() * 1000)   # lunes: apertura de Europa


def bar(t, o, h, l, c, v=10.0, tb=5.0):
    return [t, o, h, l, c, v, v * c, tb]


def flat(t0, n, px=100.0, v=10.0, tb=5.0):
    return [bar(t0 + k * MIN, px, px + 0.1, px - 0.1, px, v, tb) for k in range(n)]


def session_bars(break_close=101.0, break_v=60.0, break_tb=50.0):
    """80 velas planas antes de la apertura, 5 de rango (99,5–100,5) y una de ruptura al alza."""
    bars = flat(T0 - 80 * MIN, 80)
    bars += [bar(T0 + k * MIN, 100, 100.5, 99.5, 100) for k in range(5)]
    bars.append(bar(T0 + 5 * MIN, 100.4, max(101.2, break_close), 100.0, break_close, break_v, break_tb))
    return bars


# ---------------------------------------------------------------- velas e indicadores

def test_to_bars_formato_y_sin_vela_en_curso():
    raw = [[1000, "1", "2", "0.5", "1.5", "10", 60999, "15", 7, "6", "9", "0"],
           [61000, "1.5", "2", "1", "1.8", "4", 120999, "7", 3, "1", "2", "0"]]
    assert ap.to_bars(raw, now=100_000) == [[1000, 1.0, 2.0, 0.5, 1.5, 10.0, 15.0, 6.0]]
    assert len(ap.to_bars(raw)) == 2


def test_rango_de_apertura_y_volumen():
    bars = session_bars()
    r = ap.opening_range(bars, T0, 5)
    assert r == {"hi": 100.5, "lo": 99.5, "vol": 50.0, "n": 5}
    assert ap.opening_range(bars, T0 + 60 * MIN, 5) is None
    assert ap.slot_volume(bars, T0 - 10 * MIN, T0) == 100.0


def test_rvol_con_sesiones_anteriores_y_sin_historia():
    bars = session_bars()
    t = T0 + 6 * MIN
    # Sin historia: últimos 5 min (4×10 + 60 = 100) frente a la media de los 60 min previos (10/min → 50).
    assert ap.rvol(bars, t, 5) == pytest.approx(2.0)
    # Con sesiones anteriores: frente a su mediana (40 y 60 → 50; un pánico de 900 no la infla); una sola no basta.
    assert ap.rvol(bars, t, 5, prior=[40.0, 60.0]) == pytest.approx(2.0)
    assert ap.rvol(bars, t, 5, prior=[25.0, 25.0, 0.0]) == pytest.approx(4.0)
    assert ap.rvol(bars, t, 5, prior=[50.0, 40.0, 900.0]) == pytest.approx(2.0)
    assert ap.rvol(bars, t, 5, prior=[1000.0]) == pytest.approx(2.0)
    assert ap.rvol([], t, 5) is None


def test_prior_volumes_mismo_tramo_relativo_a_cada_apertura():
    calls = []
    vs = lambda a, b: calls.append((a, b)) or 7.0
    prev = [T0 - 86400_000, T0 - 3 * 86400_000]
    assert ap.prior_volumes(vs, T0, prev, T0 + 20 * MIN, 15) == [7.0, 7.0]
    assert calls[0] == (prev[0] + 5 * MIN, prev[0] + 20 * MIN)


def test_atr_y_presion_compradora():
    bars = session_bars()
    a = ap.atr(bars, 14)
    assert a == pytest.approx((8 * 0.2 + 5 * 1.0 + 1.2) / 14)
    assert ap.atr(bars[:1]) is None
    assert ap.buy_ratio(bars, 5) == pytest.approx((4 * 5 + 50) / (4 * 10 + 60))
    assert ap.buy_ratio(bars, 5, end=80) == pytest.approx(0.5)
    assert ap.buy_ratio([bar(0, 1, 1, 1, 1, 0.0, 0.0)], 5) is None


def test_features_solo_miran_hacia_atras():
    bars = session_bars()
    i = len(bars) - 1
    f = ap.features(bars, i, T0)
    assert f["rvol"] == pytest.approx(2.0) and f["buy_ratio"] == pytest.approx(0.7)
    g = ap.features(bars + [bar(T0 + 6 * MIN, 101, 150, 50, 150, 999, 999)], i, T0)
    assert g == f


# ---------------------------------------------------------------- entrada y stops

RNG = {"hi": 100.5, "lo": 99.5, "vol": 50.0, "n": 5}


def test_confirm_entra_solo_a_favor_con_volumen_y_presion():
    up = bar(T0, 100.4, 101.2, 100, 101.0)
    ok = ap.confirm(1, up, RNG, 2.0, 0.7)
    assert ok["ok"] and ok["dir"] == 1 and "rompe el máximo" in ok["why"]
    assert not ap.confirm(-1, up, RNG, 2.0, 0.7)["ok"]                       # rompe en contra de la señal
    assert "en contra" in ap.confirm(-1, up, RNG, 2.0, 0.7)["why"]
    assert "sin volumen" in ap.confirm(1, up, RNG, 1.2, 0.7)["why"]
    assert "sin presión compradora" in ap.confirm(1, up, RNG, 2.0, 0.5)["why"]
    assert "Sin señal" in ap.confirm(0, up, RNG, 2.0, 0.7)["why"]            # sin señal no se opera en vivo
    assert ap.confirm(0, up, RNG, 2.0, 0.7, {"free": True})["dir"] == 1      # ruptura libre (solo backtest)
    inside = bar(T0, 100, 100.5, 99.6, 100.52)                              # no supera el margen (5 % del rango)
    assert not ap.confirm(1, inside, RNG, 2.0, 0.7)["ok"]
    dn = bar(T0, 99.6, 99.7, 98.9, 99.0)
    assert ap.confirm(-1, dn, RNG, 2.0, 0.3)["ok"]
    assert not ap.confirm(-1, dn, RNG, 2.0, 0.5)["ok"]
    assert not ap.confirm(1, up, None, 2.0, 0.7)["ok"]


def test_stop_inicial_el_mas_cercano_que_no_sea_ridiculo():
    s = ap.initial_stop(1, 101.0, RNG, 0.5)                 # rango 1,49 %, ATR 2×0,5 = 0,99 %
    assert s["kind"] == "atr" and s["pct"] == pytest.approx(0.99, abs=1e-3)
    assert s["price"] == pytest.approx(101 * (1 - 0.0099), rel=1e-4)
    s = ap.initial_stop(1, 101.0, RNG, 0.05)                # ATR ridículo (0,1 %): manda el rango
    assert s["kind"] == "rango" and s["pct"] == pytest.approx(1.485, abs=1e-3)
    s = ap.initial_stop(1, 100.6, {"hi": 100.5, "lo": 100.45}, 0.05)   # todo demasiado cerca: el mínimo
    assert s["kind"] == "minimo" and s["pct"] == 0.3
    s = ap.initial_stop(-1, 90.0, {"hi": 100.0, "lo": 95.0}, 10.0)    # todo muy lejos: el máximo
    assert s["kind"] == "maximo" and s["pct"] == 3.0 and s["price"] > 90


def test_trail_pct_en_atr_con_limites():
    assert ap.trail_pct(100.0, 0.1, 1.0) == pytest.approx(0.6)          # 6 ATR
    assert ap.trail_pct(100.0, 0.01, 1.0) == 0.2                         # mínimo
    assert ap.trail_pct(100.0, 5.0, 1.0) == 5.0                          # máximo
    assert ap.trail_pct(100.0, None, 1.0) == 1.0


def test_dynamic_stop_como_el_paper():
    # Largo a 100 con stop 1 % y trailing 0,5 %: la distancia baja de 1 % a 0,5 % según gana (+0,3 % → 0,7 %);
    # en +1 % (1 R) break-even y trailing a 0,5 %.
    stop, be, k = ap.dynamic_stop(1, 100.0, 1.0, 0.5, 100.3, 99.0, False)
    assert stop == pytest.approx(100.3 * (1 - 0.007)) and not be and k == "dinamico"
    stop, be, k = ap.dynamic_stop(1, 100.0, 1.0, 2.0, 100.3, 99.0, False)   # trailing ancho: no se mueve aún
    assert (stop, be, k) == (99.0, False, None)
    stop, be, k = ap.dynamic_stop(1, 100.0, 1.0, 0.5, 101.0, 99.0, False)
    assert be and k == "dinamico" and stop == pytest.approx(101 * 0.995)
    stop2, _, k2 = ap.dynamic_stop(1, 100.0, 1.0, 0.5, 100.8, stop, True)   # nunca retrocede
    assert stop2 == stop and k2 is None
    # Corto: el stop baja con el mínimo favorable.
    stop, be, k = ap.dynamic_stop(-1, 100.0, 1.0, 0.5, 98.0, 101.0, False)
    assert be and stop == pytest.approx(98 * 1.005)
    # Sin trailing: solo el break-even a +1 R.
    stop, be, k = ap.dynamic_stop(1, 100.0, 1.0, None, 101.0, 99.0, False)
    assert (stop, be, k) == (100.0, True, "be")


# ---------------------------------------------------------------- reglas de una moneda

F_OK = {"rvol": 2.0, "rvol15": 1.3, "buy_ratio": 0.7, "atr": 0.5}


def test_coinrun_rango_entrada_reentrada_y_dos_perdidas():
    p = {"range_min": 5}
    run = ap.CoinRun("BTC", T0, 1, p)
    bars = session_bars()
    i = ap.bar_index(bars, T0)
    for k in range(i, i + 5):
        assert run.on_bar(bars, k, F_OK) is None
    assert run.state == "armada" and run.rng["hi"] == 100.5
    act = run.on_bar(bars, i + 5, F_OK)
    assert act["action"] == "enter" and act["dir"] == 1 and act["n"] == 1 and run.state == "en_posicion"
    assert 0.3 <= act["stopPct"] <= 3 and act["trailPct"] >= 0.2 and "Entrada" in act["why"]
    assert run.on_bar(bars, i + 5, F_OK) is None            # en posición manda el stop
    run.on_exit(T0 + 9 * MIN + 20_000, -0.4, "Stop de pérdida")
    assert run.state == "rearmada" and run.rng is None and run.rng_start == T0 + 10 * MIN
    # Rango tras la salida (5 min) y nueva ruptura a favor.
    more = [bar(T0 + (10 + k) * MIN, 100.8, 101.0, 100.6, 100.8) for k in range(5)]
    more.append(bar(T0 + 15 * MIN, 100.9, 101.6, 100.8, 101.5))
    bars2 = bars + [bar(T0 + (6 + k) * MIN, 100.8, 100.9, 100.7, 100.8) for k in range(4)] + more
    j = ap.bar_index(bars2, T0 + 10 * MIN)
    for k in range(j, j + 5):
        assert run.on_bar(bars2, k, F_OK) is None
    act = run.on_bar(bars2, j + 5, F_OK)
    assert act["action"] == "enter" and act["n"] == 2 and "Reentrada 1" in act["why"]
    run.on_exit(T0 + 20 * MIN, -0.2, "Stop de pérdida")
    assert run.done and "2 pérdidas seguidas" in run.end_why


def test_coinrun_rearmada_exige_volumen_y_maximo_de_reentradas():
    run = ap.CoinRun("ETH", T0, -1, {"range_min": 5, "max_reentries": 1})
    run.state, run.entries, run.rng = "en_posicion", 1, dict(RNG)
    run.on_exit(T0 + 30 * MIN, 1.2, "Stop dinámico")
    assert run.state == "rearmada" and run.losses_row == 0
    bars = flat(T0 + 30 * MIN, 5) + [bar(T0 + 35 * MIN, 99.9, 99.9, 98.8, 98.9, tb=1.0)]
    for k in range(5):
        run.on_bar(bars, k, F_OK)
    weak = {**F_OK, "rvol15": 0.9, "buy_ratio": 0.2}
    assert run.on_bar(bars, 5, weak) is None and "no llega" in run.last_why   # rvol 15 min < 1
    act = run.on_bar(bars, 5, {**weak, "rvol15": 1.2})
    assert act["action"] == "enter" and act["dir"] == -1
    run.on_exit(T0 + 40 * MIN, 0.5, "Stop dinámico")
    assert run.done and "máximo de 1 reentradas" in run.end_why


def test_coinrun_fin_por_volumen_agotado_tiempo_y_sin_ruptura():
    bars = session_bars()
    i = ap.bar_index(bars, T0)
    run = ap.CoinRun("SOL", T0, 1, {"range_min": 5})
    for k in range(i, i + 5):
        run.on_bar(bars, k, F_OK)
    assert run.on_bar(bars, i + 5, {**F_OK, "rvol15": 0.5})["action"] == "enter"   # antes de operar no corta
    run.on_exit(T0 + 8 * MIN, 0.6, "Stop dinámico")
    after = bars + [bar(T0 + 8 * MIN, 100, 100.1, 99.9, 100)]
    assert run.on_bar(after, len(after) - 1, {**F_OK, "rvol15": 0.5})["action"] == "end"
    assert "Volumen agotado" in run.end_why
    late = [bar(T0 + 91 * MIN, 100, 100.1, 99.9, 100)]
    run = ap.CoinRun("SOL", T0, 1, {"range_min": 5})
    run.rng, run.state = dict(RNG), "armada"
    assert run.on_bar(late, 0, F_OK)["action"] == "end" and "primeros 90 min" in run.end_why
    run = ap.CoinRun("SOL", T0, 1, {"range_min": 5})
    assert run.on_bar([bar(T0 + 239 * MIN, 1, 1, 1, 1)], 0, F_OK)["action"] == "end" and "4 h" in run.end_why


def test_coinrun_vela_vieja_no_entra_y_rechazo_espera():
    bars = session_bars()
    i = ap.bar_index(bars, T0)
    run = ap.CoinRun("XRP", T0, 1, {"range_min": 5})
    for k in range(i, i + 5):
        run.on_bar(bars, k, F_OK)
    assert run.on_bar(bars, i + 5, F_OK, live_ok=False) is None and "tarde" in run.last_why
    act = run.on_bar(bars, i + 5, F_OK)
    assert act["action"] == "enter"
    run.on_rejected(T0 + 6 * MIN, "vetada")
    assert run.state == "armada" and run.entries == 0 and run.wait_until == T0 + 11 * MIN
    assert run.on_bar(bars, i + 5, F_OK) is None              # pausa tras el rechazo


def test_coinrun_tras_salir_no_mira_velas_de_cuando_habia_posicion():
    run = ap.CoinRun("BTC", T0, 1, {"range_min": 5})
    run.state, run.entries, run.rng = "en_posicion", 1, dict(RNG)
    run.on_exit(T0 + 40 * MIN + 5000, 0.8, "Stop dinámico")
    assert run.state == "rearmada" and run.resume_ms == T0 + 41 * MIN
    old = [bar(T0 + 20 * MIN, 100, 100.1, 99.9, 100)]
    assert run.on_bar(old, 0, {**F_OK, "rvol15": 0.3}) is None and run.state == "rearmada"   # no corta por volumen
    new = [bar(T0 + 41 * MIN, 100, 100.1, 99.9, 100)]
    assert run.on_bar(new, 0, {**F_OK, "rvol15": 0.3})["action"] == "end" and "Volumen agotado" in run.end_why


def test_coinrun_sin_entradas_nuevas_al_final_de_la_sesion():
    run = ap.CoinRun("BTC", T0, 1, {"range_min": 5})
    run.state, run.entries, run.rng = "en_posicion", 1, dict(RNG)
    run.on_exit(T0 + 215 * MIN, 0.8, "Stop dinámico")              # quedan 25 min
    assert run.done and "poca sesión" in run.end_why
    run = ap.CoinRun("BTC", T0, 1, {"range_min": 5})
    run.state, run.entries, run.rng = "en_posicion", 1, dict(RNG)
    run.on_exit(T0 + 150 * MIN, 0.8, "Stop dinámico")
    assert run.state == "rearmada"
    late = [bar(T0 + 211 * MIN, 100, 100.1, 99.9, 100)]
    assert run.on_bar(late, 0, F_OK)["action"] == "end" and "menos de 30 min" in run.end_why


def test_coinrun_senal_que_deja_de_acompanar():
    run = ap.CoinRun("BTC", T0, 1)
    run.set_signal(-1)
    assert run.dir == -1 and not run.done                     # antes de entrar se sigue la nueva
    run.entries = 1
    run.set_signal(0)
    assert run.done and "ya no acompaña" in run.end_why


# ---------------------------------------------------------------- agenda

def test_aperturas_agrupadas_con_cambio_de_hora():
    o = {x["id"]: x for x in ap.openings_on(date(2026, 10, 26))}
    assert set(o) == {"tokio-seul", "china", "europa", "nuevayork", "saopaulo"}   # Dubái solo es referencia
    hm = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%H:%M")
    assert o["europa"]["markets"] == ["frankfurt", "londres", "madrid"] and hm(o["europa"]["open_ms"]) == "08:00"
    assert hm(o["china"]["open_ms"]) == "01:30" and o["china"]["name"] == "Shanghái y Hong Kong"
    assert hm(o["nuevayork"]["open_ms"]) == "13:30"       # EE. UU. cambia de hora el 1 de noviembre
    assert {x["id"]: hm(x["open_ms"]) for x in ap.openings_on(date(2026, 11, 2))}["nuevayork"] == "14:30"
    assert hm({x["id"]: x for x in ap.openings_on(date(2026, 7, 1))}["europa"]["open_ms"]) == "07:00"
    assert ap.openings_on(date(2026, 10, 24)) == []       # sábado


def test_festivos_de_toda_la_ventana_no_se_operan():
    ids = lambda d: {x["id"]: x for x in ap.openings_on(d)}
    o = ids(date(2026, 11, 26))                              # Acción de Gracias
    assert "nuevayork" not in o and "saopaulo" in o and "europa" in o
    o = ids(date(2026, 12, 25))
    assert not {"europa", "nuevayork", "saopaulo"} & set(o)
    assert o["china"]["holiday"] == ["hongkong"] and o["tokio-seul"]["holiday"] == ["seul"]   # Shanghái y Tokio abren
    assert "china" not in ids(date(2026, 2, 17))            # Año Nuevo chino
    assert "tokio-seul" not in ids(date(2026, 5, 5))        # Golden Week y Día del Niño en Corea
    o = ids(date(2026, 5, 4))                                # festivo solo en Londres: Europa sigue
    assert o["europa"]["markets"] == ["frankfurt", "londres", "madrid"] and o["europa"]["holiday"] == ["londres"]
    assert ap.market_open_on("nuevayork", date(2026, 11, 27)) and not ap.market_open_on("nuevayork", date(2026, 11, 26))


def test_sesiones_anteriores_saltan_el_fin_de_semana():
    prev = ap.prev_session_opens("nuevayork", int(datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc).timestamp() * 1000), 3)
    days = [datetime.fromtimestamp(x / 1000, timezone.utc).strftime("%d %H:%M") for x in prev]
    assert days == ["30 13:30", "29 13:30", "28 13:30"]


# ---------------------------------------------------------------- señal

def news_ev(ts, d, mag=60, cred=80, bolsas=(), activos=(), nivel="nacional", tipo="macro", imp=7, reflejo="confirmada",
            clase=None):
    """Triada de una noticia macro (por defecto, ya confirmada por la cripto: bot/reflejo.py; reflejo=None, sin medir).
    clase: la del prefiltro de su captada (la pone signal_getter con story_sources); sin ella, la regla del titular."""
    ev = {"ts": ts, "kind": "triaged", "story": f"n{ts}", "patch": {"triage": {
        "tipo": tipo, "credibilidad": cred, "alcance": {"nivel": nivel, "puntuacion": 50}, "importancia": imp,
        "repercusion": {"direccion": d, "magnitud": mag, "activos": list(activos), "bolsas": list(bolsas)},
        "monedas": [a for a in activos if a in ("BTC", "ETH", "SOL", "XRP")], "resumen": f"Noticia {ts}"}}}
    if reflejo:
        ev["reflejo"] = {"estado": reflejo, "mov_pct": 0.6 if d > 0 else -0.6, "min": 12, "ref": "media"}
    if clase:
        ev["clase"] = clase
    return ev


def plan_ev(ts, coin, side, win="Apertura Londres", llm=None, src="Estratega · Reglas"):
    return {"ts": ts, "kind": "strategy", "agent": "strat", "story": f"w-apertura-londres-2026-10-26-{coin.lower()}",
            "patch": {"src": src, "strat": {"coin": coin, "side": side, "timing": "window", "win": win,
                                            "winAt": T0 + 5 * MIN, "llm": llm}}}


def test_senal_con_plan_noticias_y_predicciones():
    markets = ["frankfurt", "londres", "madrid"]
    now = T0 - 15 * MIN
    plans = [plan_ev(now - 60_000, "BTC", "long", llm={"model": "x"})]
    news = [news_ev(now - 30 * 60_000, 60, bolsas=["londres"]), news_ev(now - 10 * 60_000, -50, bolsas=["tokio"])]
    pred = {"items": [{"kind": "cripto", "target": "BTC", "label": "BTC · Apertura Londres", "id": "BTC-londres-2026-10-26",
                       "when_utc": T0, "prob_up": 0.66, "confidence": "media"},
                      {"kind": "bolsa", "target": "londres", "when_utc": T0, "prob_up": 0.6, "confidence": "alta"},
                      {"kind": "cripto", "target": "BTC", "label": "BTC · próxima hora", "id": "BTC-1h-x",
                       "when_utc": T0, "prob_up": 0.1, "confidence": "alta"}]}
    s = ap.signal_from("BTC", markets, T0, plans, news, pred, now)
    assert s["dir"] == 1 and 0.4 < s["strength"] <= 1 and "verde" in s["why"]
    assert len(s["drivers"]) == 3 and s["drivers"][0]["peso"] == pytest.approx(0.35)
    assert "1 noticia" in s["drivers"][1]["factor"]               # la de Tokio no cuenta para Europa
    e = ap.signal_from("ETH", markets, T0, plans, news, None, now)  # el plan no incluye ETH: una noticia sola no basta
    assert e["dir"] == 0 and 0.1 < e["strength"] < 0.2 and any("no incluye ETH" in d["factor"] for d in e["drivers"])
    none = ap.signal_from("XRP", ["nuevayork"], T0, [], [], None, now)
    assert none["dir"] == 0 and "Sin señal clara" in none["why"]
    # La predicción de la bolsa (el índice) ya no cuenta: solo la de la ventana cripto de la moneda
    assert s["drivers"][2]["factor"] == "Predicción de subida: BTC · Apertura Londres 66 %"
    assert s["drivers"][2]["peso"] == pytest.approx(ap.W_PRED * 0.32 * 0.8)
    solo_bolsa = {"items": [pred["items"][1]]}
    assert ap.signal_from("BTC", markets, T0, [], [], solo_bolsa, now)["drivers"] == []
    # Una macro de la clase nueva (captada sin monedas) confirmada vale para las cuatro y para cualquier ventana
    macro = [news_ev(now - 10 * 60_000, -50, bolsas=["tokio"], clase="macro")]
    assert "1 noticia" in ap.signal_from("ETH", markets, T0, [], macro, None, now)["drivers"][0]["factor"]
    sin = [news_ev(now - 10 * 60_000, -50, bolsas=["tokio"], clase="macro", reflejo="pendiente")]
    assert ap.signal_from("ETH", markets, T0, [], sin, None, now)["drivers"] == []


def test_senal_roja_globales_decaimiento_y_eventos_propios():
    now = T0
    bad = [news_ev(now - 20 * 60_000, -90, mag=90, cred=90, nivel="global")]
    s = ap.signal_from("SOL", ["nuevayork"], T0, [], bad, None, now)
    assert s["dir"] == -1 and "roja" in s["why"]
    old = [news_ev(now - 5 * 3600_000, -90, mag=90, cred=90, nivel="global")]       # vida media de 2 h
    assert ap.signal_from("SOL", ["nuevayork"], T0, [], old, None, now)["strength"] < s["strength"] / 3
    other = [news_ev(now - 60_000, -90, mag=90, cred=90, nivel="global", activos=["XRP"])]   # habla de otra moneda
    assert ap.signal_from("SOL", ["nuevayork"], T0, [], other, None, now)["dir"] == 0
    xrp_ny = [news_ev(now - 60_000, -90, mag=90, cred=90, bolsas=["nuevayork"], activos=["XRP"])]
    assert ap.signal_from("BTC", ["nuevayork"], T0, [], xrp_ny, None, now)["drivers"] == []   # aunque coincida la bolsa
    assert ap.signal_from("XRP", ["nuevayork"], T0, [], xrp_ny, None, now)["dir"] == -1
    low = [news_ev(now - 60_000, -90, mag=90, cred=90, nivel="global", imp=2)]               # archivada por importancia
    assert ap.signal_from("SOL", ["nuevayork"], T0, [], low, None, now)["dir"] == 0
    mine = [plan_ev(now - 60_000, "BTC", "short", src=ap.SRC)]                              # nuestro propio evento
    assert ap.signal_from("BTC", ["londres"], T0, mine, [], None, now)["drivers"] == []
    nostrat = [{"ts": now - 60_000, "kind": "nostrategy", "story": "w-apertura-londres-2026-10-26",
                "patch": {"strat": {"none": True, "why": "nada"}}}]
    d = ap.signal_from("BTC", ["londres"], T0, nostrat, [], None, now)["drivers"]
    assert d and d[0]["peso"] == 0 and "no ve operación" in d[0]["factor"]


class FakeStore:
    def __init__(self, evs):
        self.evs = evs

    def events_since(self, ts, kinds):
        return [e for e in self.evs if e["ts"] >= ts and e["kind"] in kinds]


class FakeBus:
    def __init__(self, evs=()):
        self.store = FakeStore(list(evs))
        self.state = {}
        self.msgs = []

    def publish(self, m):
        self.msgs.append(m)
        if m["type"] != "event":
            self.state[m["type"]] = m
        return m


def test_signal_getter_lee_el_bus():
    bus = FakeBus([plan_ev(T0 - 60_000, "BTC", "long", llm={"m": 1}), news_ev(T0 - 60_000, 70, bolsas=["londres"])])
    get = ap.signal_getter(bus, clock=lambda: T0 - 10_000)
    s = get("BTC", {"markets": ["londres"], "open_ms": T0})
    assert s["dir"] == 1 and len(s["drivers"]) == 2


# ---------------------------------------------------------------- motor en vivo

def live_bars():
    bars = session_bars()
    bars += [bar(T0 + (6 + k) * MIN, 101, 101.3, 100.9, 101.1) for k in range(30)]
    return bars


async def test_motor_arma_entra_publica_y_rearma():
    now = [T0 - 20 * MIN]
    bus = FakeBus()
    data = live_bars()
    sent, pf = [], {"positions": [], "pending": [], "closed": []}

    def get_signal(coin, w):
        assert w["markets"] and w["open_ms"]
        return ({"dir": 1, "strength": 0.6, "drivers": [{"factor": "x", "peso": 0.6}], "why": "verde"} if coin == "BTC"
                else {"dir": 0, "strength": 0.0, "drivers": [], "why": "sin señal"})

    def submit(ticket, story, headline, strategy, window):
        sent.append((ticket, story, headline, strategy, window))
        return True

    eng = ap.OpeningEngine(bus, None, get_signal, submit, lambda: pf, clock=lambda: now[0], params={"range_min": 5})

    async def fetch(coin, start, limit):
        return [b for b in data if start <= b[0] < start + limit * MIN and b[0] + MIN <= now[0]]

    eng.fetch_klines = fetch
    await eng.step()
    w = next(x for x in eng.status["windows"] if x["id"].startswith("europa-"))
    assert w["state"] == "esperando" and w["markets"] == ["frankfurt", "londres", "madrid"]
    now[0] = T0 - 10 * MIN
    await eng.step()
    w = next(x for x in eng.view()["windows"] if x["id"].startswith("europa-"))
    assert w["state"] == "armada" and w["coins"]["BTC"]["signal"] == 1 and w["coins"]["ETH"]["signal"] == 0
    now[0] = T0 + 7 * MIN + 3000
    await eng.step()
    assert len(sent) == 1
    t, story, hl, strat, win = sent[0]
    assert story == "a-europa-2026-10-26-btc-1" and strat == ap.STRATEGY and win == "Apertura Europa"
    assert t["timing"] == "now" and t["side"] == "long" and t["mode"] == ap.MODE and t["winEnd"] == T0 + 240 * MIN
    assert 0.3 <= t["stopPct"] <= 3 and t["trailPct"] > 0 and 0 < t["risk"] <= 0.5 and t["market"] == "londres"
    ev = next(m for m in bus.msgs if m["type"] == "event")
    assert ev["kind"] == "strategy" and ev["agent"] == "strat" and ev["patch"]["src"] == ap.SRC
    assert ev["patch"]["strat"]["coin"] == "BTC" and "señal verde" in ev["patch"]["headline"]
    w = next(x for x in eng.view()["windows"] if x["id"].startswith("europa-"))
    assert w["state"] == "en_posicion" and w["coins"]["BTC"]["entries"] == 1 and w["coins"]["BTC"]["range"]["hi"] == 100.5
    assert bus.state["openings"]["type"] == "openings"
    # El paper la abre y luego la cierra con pérdida: se rearma.
    pf["positions"] = [{"story": story}]
    now[0] += 10_000
    await eng.step()
    pf["positions"], pf["closed"] = [], [{"story": story, "closed": T0 + 9 * MIN, "pnl_pct": -0.35, "reason": "Stop"}]
    now[0] = T0 + 10 * MIN
    await eng.step()
    run = eng.windows[next(k for k in eng.windows if k.startswith("europa-"))]["runs"]["BTC"]
    assert run.state == "rearmada" and run.results == [-0.35]


async def test_motor_orden_bloqueada_y_fallo_de_datos_no_tumba():
    now = [T0 + 7 * MIN + 3000]
    bus = FakeBus()
    data = live_bars()
    sent = []
    eng = ap.OpeningEngine(bus, None, lambda c, w: {"dir": 1 if c == "BTC" else 0, "strength": 0.5, "why": ""},
                           lambda *a: sent.append(a) or True, lambda: {"positions": [], "closed": []},
                           clock=lambda: now[0], params={"range_min": 5})
    fails = {"n": 0}

    async def fetch(coin, start, limit):
        if fails["n"] < 1:
            fails["n"] += 1
            raise ConnectionError("caída")
        return [b for b in data if start <= b[0] < start + limit * MIN and b[0] + MIN <= now[0]]

    eng.fetch_klines = fetch
    await eng.step()                       # primer fallo: espera creciente, sin excepción
    assert not sent and eng._retry_at > now[0]
    now[0] = T0 + 40 * MIN                 # los datos vuelven tarde: la ruptura ya es vieja y no se entra
    await eng.step()
    assert not sent
    run = eng.windows[next(k for k in eng.windows if k.startswith("europa-"))]["runs"]["BTC"]
    assert run.state == "armada" and run.entries == 0


def _engine(data, now, pf, sig=None, bus=None, sent=None):
    """Motor con velas fijas (sin red), señal verde solo en BTC y cartera pf (dict que la prueba va cambiando)."""
    sent = [] if sent is None else sent
    sig = sig or (lambda c, w: {"dir": 1 if c == "BTC" else 0, "strength": 0.6, "why": ""})
    eng = ap.OpeningEngine(bus or FakeBus(), None, sig, lambda *a: sent.append(a) or True, lambda: pf,
                           clock=lambda: now[0], params={"range_min": 5})

    async def fetch(coin, start, limit):
        return [b for b in data if start <= b[0] < start + limit * MIN and b[0] + MIN <= now[0]]

    eng.fetch_klines = fetch
    return eng, sent


def _run(eng, coin="BTC"):
    return eng.windows[next(k for k in eng.windows if k.startswith("europa-"))]["runs"][coin]


async def test_motor_tras_salir_no_corta_por_velas_de_cuando_habia_posicion():
    """Poco volumen durante la posición y mucho después: tras salir a T0+40 se rearma (como en el backtest)."""
    data = session_bars()
    data += [bar(T0 + k * MIN, 101, 101.2, 100.9, 101.1, v=2.0, tb=1.0) for k in range(6, 20)]
    data += [bar(T0 + k * MIN, 101, 101.2, 100.9, 101.1, v=30.0, tb=15.0) for k in range(20, 60)]
    now, pf = [T0 + 7 * MIN + 3000], {"positions": [], "pending": [], "closed": []}
    eng, sent = _engine(data, now, pf)
    await eng.step()
    story = sent[0][1]
    pf["positions"] = [{"story": story, "coin": "BTC", "side": "long"}]
    now[0] = T0 + 8 * MIN
    await eng.step()
    pf["positions"], pf["closed"] = [], [{"story": story, "opened": T0 + 7 * MIN + 3000, "closed": T0 + 40 * MIN,
                                          "pnl_pct": -0.3, "reason": "Stop"}]
    now[0] = T0 + 41 * MIN + 3000
    await eng.step()
    run = _run(eng)
    assert run.state == "rearmada" and run.results == [-0.3] and not run.done, run.last_why


async def test_motor_reiniciado_recupera_la_sesion_y_no_repite_story():
    """Reinicio con una operación ya cerrada en esta apertura: no reutiliza su story ni la toma por la nueva."""
    data = session_bars()
    data += [bar(T0 + k * MIN, 101, 101.1, 100.9, 101.0) for k in range(6, 28)]
    data.append(bar(T0 + 28 * MIN, 101.0, 102.1, 101.0, 102.0, 60.0, 50.0))
    old = {"story": "a-europa-2026-10-26-btc-1", "opened": T0 + 6 * MIN, "closed": T0 + 20 * MIN, "pnl_pct": -0.5,
           "reason": "Stop de pérdida"}
    now, pf = [T0 + 29 * MIN + 30_000], {"positions": [], "pending": [], "closed": [old]}
    eng, sent = _engine(data, now, pf)
    await eng.step()
    run = _run(eng)
    assert run.results == [-0.5] and run.losses_row == 1 and run.entries == 2
    assert len(sent) == 1 and sent[0][1] == "a-europa-2026-10-26-btc-2"
    pf["positions"] = [{"story": sent[0][1], "coin": "BTC", "side": "long"}]
    now[0] += 10_000
    await eng.step()
    assert run.state == "en_posicion" and run.results == [-0.5]
    # Una cerrada antigua con la misma story que una orden nueva no cuenta como su salida.
    w = eng.windows[next(k for k in eng.windows if k.startswith("europa-"))]
    w["stories"]["a-europa-2026-10-26-btc-2"]["at"] = now[0]
    pf["closed"] = [old, {**old, "story": "a-europa-2026-10-26-btc-2"}]
    await eng.step()
    assert run.state == "en_posicion" and run.results == [-0.5]


async def test_motor_una_sola_posicion_por_moneda():
    now = [T0 + 7 * MIN + 3000]
    pf = {"positions": [{"story": "w-apertura-londres-2026-10-26-btc", "coin": "BTC", "side": "short"}],
          "pending": [], "closed": []}
    eng, sent = _engine(live_bars(), now, pf)
    await eng.step()
    run = _run(eng)
    assert not sent and run.entries == 0 and run.rejects == 1 and run.state == "armada"
    assert "ya hay una posición abierta en BTC (venta" in run.last_why


async def test_motor_orden_pendiente_que_desaparece_es_un_rechazo():
    now, pf = [T0 + 7 * MIN + 3000], {"positions": [], "pending": [], "closed": []}
    eng, sent = _engine(live_bars(), now, pf)
    await eng.step()
    pf["pending"] = [{"story": sent[0][1], "coin": "BTC", "side": "long"}]
    now[0] += 5000
    await eng.step()
    pf["pending"] = []                                         # vetada o caducada: sale sin pasar por cerradas
    now[0] += 30_000
    await eng.step()
    run = _run(eng)
    assert run.entries == 0 and run.results == [] and run.losses_row == 0 and run.state == "armada"
    assert "no llegó a ejecutarla" in run.last_why


async def test_motor_fallo_al_preparar_la_orden_no_bloquea_la_moneda():
    class BadBus(FakeBus):
        def publish(self, m):
            if m["type"] == "event":
                raise RuntimeError("bus caído")
            return super().publish(m)

    bus = BadBus()
    now = [T0 + 7 * MIN + 3000]
    eng, sent = _engine(live_bars(), now, {"positions": [], "closed": []}, bus=bus)
    await eng.step()
    run = _run(eng)
    assert not sent and run.state == "armada" and run.entries == 0 and "fallo interno" in run.last_why
    assert bus.state["openings"]["type"] == "openings"          # el resto de la ronda sigue


async def test_motor_no_arma_aperturas_que_pasaron_con_el_bot_parado():
    calls = []
    now = [T0 + 5 * 3600_000]                                  # la sesión de Europa acabó hace 1 h
    eng, sent = _engine(live_bars(), now, {"positions": [], "closed": []},
                        sig=lambda c, w: calls.append(w["id"]) or {"dir": 1, "strength": 0.9, "why": "verde"})
    await eng.step()
    assert "europa" not in calls and not sent
    w = next(x for x in eng.view()["windows"] if x["id"].startswith("europa-"))
    assert w["state"] == "terminada" and "bot parado" in w["why"]
    assert w["coins"]["BTC"]["signal"] == 0 and w["coins"]["BTC"]["state"] == "terminada"


# ---------------------------------------------------------------- v2: velas de 5 min, stops, objetivo y puertas

V2_STOP = {"stop_mode": "or_mid", "max_cost_r": 0.25}


def test_velas_de_5_min_cerradas_y_atr5():
    bars = [bar(T0 - (20 - k) * MIN, 100 + k * 0.1, 100.5 + k * 0.1, 99.5, 100 + k * 0.1) for k in range(23)]
    b5 = ap.bars5(bars, len(bars), 10)                       # las 3 de T0..T0+2 son de una vela de 5 min sin cerrar
    assert [b[0] for b in b5] == [T0 - 20 * MIN, T0 - 15 * MIN, T0 - 10 * MIN, T0 - 5 * MIN]
    assert b5[0][1:6] == pytest.approx([100.0, 100.9, 99.5, 100.4, 50.0])
    assert ap.bars5(bars, 20, 10)[-1][0] == T0 - 5 * MIN     # hasta bars[19] (T0−1): la de T0−5 ya cerró
    assert ap.bars5(bars, len(bars), 2) == b5[-2:]
    tr = [max(b[2] - b[3], abs(b[2] - a[4]), abs(b[3] - a[4])) for a, b in zip(b5, b5[1:])]
    assert ap.atr(ap.bars5(bars, len(bars), 15), 14) == pytest.approx(sum(tr) / 3)
    f = ap.features(bars, len(bars) - 1, T0, params={"trail_tf": 5})
    assert f["atr5"] == pytest.approx(sum(tr) / 3) and "atr5" not in ap.features(bars, len(bars) - 1, T0)


def five_min_bars(last_close=101.1):
    """Rango de 5 min (99,5–100,5), ruptura de 1 min a T0+5 y cuatro velas más hasta cerrar la vela de 5 min."""
    return session_bars() + [bar(T0 + (6 + k) * MIN, 101, 101.3, 100.3, last_close if k == 3 else 101.1)
                             for k in range(4)]


def test_confirmacion_al_cierre_de_la_vela_de_5_min():
    bars = five_min_bars()
    i = ap.bar_index(bars, T0)
    run = ap.CoinRun("BTC", T0, 1, {"range_min": 5, "confirm_tf": 5})
    for k in range(i, i + 9):                                # la ruptura de 1 min (T0+5) no basta
        assert run.on_bar(bars, k, F_OK) is None
    act = run.on_bar(bars, i + 9, F_OK)                      # cierre de la vela de 5 min T0+5..T0+10
    assert act["action"] == "enter" and act["t"] == T0 + 10 * MIN and act["price"] == 101.1 and act["tf"] == 5
    assert "Cierre de 5 min" in act["why"]
    back = five_min_bars(last_close=100.45)                  # rompe con 1 min pero la vela de 5 min cierra dentro
    run = ap.CoinRun("BTC", T0, 1, {"range_min": 5, "confirm_tf": 5})
    assert all(run.on_bar(back, k, F_OK) is None for k in range(i, i + 10)) and "Sin ruptura" in run.last_why
    run = ap.CoinRun("BTC", T0, 1, {"range_min": 5, "confirm_tf": 5})
    for k in range(i, i + 9):
        run.on_bar(bars, k, F_OK)
    assert run.on_bar(bars, i + 9, F_OK, live_ok=False) is None and "5 min antigua" in run.last_why


def test_entrada_tardia_una_vela_de_antiguedad_como_maximo():
    mk = lambda p: ap.OpeningEngine(FakeBus(), None, lambda c, w: {}, lambda *a: True, lambda: {}, params=p)
    assert mk({})._lag_ms == ap.LIVE_LAG_MS
    assert mk({"confirm_tf": 5})._lag_ms == ap.LIVE_LAG_MS + 4 * MIN


def test_stop_estructural_con_suelo_de_costes_y_techo():
    assert ap.stop_floor_pct({}) == 0.3 and ap.stop_floor_pct(V2_STOP) == pytest.approx(0.96)   # 0,24 % / 0,25
    s = ap.initial_stop(1, 101.0, RNG, 0.5, V2_STOP)          # medio del rango: 100
    assert s["kind"] == "or_mid" and s["pct"] == pytest.approx(0.99, abs=1e-3)
    assert s["price"] == pytest.approx(100, abs=1e-3)
    s = ap.initial_stop(-1, 99.0, RNG, None, V2_STOP)
    assert s["kind"] == "or_mid" and s["price"] == pytest.approx(100, abs=1e-3)
    s = ap.initial_stop(1, 100.6, RNG, None, V2_STOP)         # el medio queda a 0,6 %: suelo de costes
    assert s["kind"] == "minimo" and s["pct"] == pytest.approx(0.96) and "estructural" in s["why"]
    s = ap.initial_stop(1, 101.0, RNG, None, {**V2_STOP, "stop_mode": "or_far"})
    assert s["kind"] == "or_far" and s["price"] == pytest.approx(99.5, abs=1e-3)
    s = ap.initial_stop(1, 101.0, RNG, 0.05, {**V2_STOP, "stop_mode": "atr5"}, atr5_v=1.0)
    assert s["kind"] == "atr5" and s["pct"] == pytest.approx(1.5 / 101 * 100, abs=1e-3)
    s = ap.initial_stop(1, 111.0, {"hi": 110, "lo": 95}, None, {**V2_STOP, "stop_mode": "or_far"})
    assert s["kind"] == "maximo" and s["pct"] == 3.0
    assert ap.trail_pct(100.0, 0.1, 1.0, {"trail_atr": 0}) is None


def _act(d, px, p, rng=RNG):
    st = ap.initial_stop(d, px, rng, None, p)
    return {"dir": d, "price": px, "stop_px": st["price"], "target_px": ap.target_price(d, px, st["pct"], p),
            "trailPct": None, "stopPct": st["pct"]}


def test_objetivo_y_puerta_rr_desde_la_entrada_real():
    p = {**V2_STOP, "target_r": 2.0, "be_r": None}
    act = _act(1, 101.0, p)
    assert act["target_px"] == pytest.approx(103.0, abs=1e-2)
    ok = ap.entry_plan(1, 101.0, act, p)
    assert ok["ok"] and ok["tp2R"] == pytest.approx(2.0, abs=1e-2) and ok["stopPct"] == pytest.approx(0.99, abs=1e-3)
    assert ok["beR"] is None and ok["tp1Frac"] == 0.0 and ok["trailStartR"] is None
    late = ap.entry_plan(1, 102.0, act, p)                    # el precio ya corrió: R:R 0,5 desde la entrada real
    assert not late["ok"] and late["rr"] == pytest.approx(0.5, abs=0.02) and "ya ha ocurrido" in late["why"]
    assert late["stopPct"] == pytest.approx(1.961, abs=1e-3)  # el stop se queda en su precio (100)
    thin = ap.entry_plan(1, 102.5, act, {**p, "min_rr": 0.1})  # 0,49 % hasta el objetivo < 3 × 0,24 %
    assert not thin["ok"] and "3 × coste" in thin["why"]
    short = _act(-1, 99.0, p)
    s = ap.entry_plan(-1, 99.0, short, p)
    assert s["ok"] and short["target_px"] == pytest.approx(97.0, abs=1e-2) and s["tp2R"] == pytest.approx(2.0, abs=1e-2)
    pct = {**V2_STOP, "target_pct": 3.0}                      # +3 % neto: 3 % más el coste de ida y vuelta
    a3 = _act(1, 101.0, pct)
    assert a3["target_px"] == pytest.approx(101 * 1.0324)
    assert ap.entry_plan(1, 101.0, a3, pct)["tp2R"] == pytest.approx(3.24 / 0.99, abs=1e-2)
    v1 = ap.entry_plan(1, 101.0, _act(1, 101.0, {}), {})      # v1: sin objetivo ni puerta R:R
    assert v1["ok"] and v1["tp2R"] is None and v1["tp1R"] == 1.0
    assert "beR" not in v1                                    # sin beR el paper usa las salidas simuladas de la v1
    for d, px in ((1, 99.8), (1, 100.0), (-1, 100.2)):        # la entrada ya cruzó el stop (100): la ruptura falló
        bad = ap.entry_plan(d, px, act if d > 0 else short, p)
        assert not bad["ok"] and "ya ha cruzado el stop" in bad["why"], (d, px)


def test_bandera_moving_el_movimiento_ya_ha_ocurrido():
    p = {"moving_pct": 3.0, "moving_h": 4, "range_min": 5}
    up = [bar(T0 - (250 - k) * MIN, 100 + k * 0.015, 100.1 + k * 0.015, 99.9 + k * 0.015, 100 + k * 0.015)
          for k in range(250)] + [bar(T0 + k * MIN, 103.7, 103.8, 103.6, 103.7) for k in range(3)]
    m = ap.moving_flag(up, T0, p)
    assert m["moving"] and m["pct"] == pytest.approx((up[249][4] / up[10][1] - 1) * 100, abs=1e-3)
    assert "ya ha ocurrido" in m["why"]
    calm = flat(T0 - 250 * MIN, 253)
    assert ap.moving_flag(calm, T0, p)["moving"] is False
    short = session_bars()                                   # solo 80 min previos: no se puede comprobar
    assert ap.moving_flag(short, T0, p)["pct"] is None and ap.moving_flag(short, T0, {}) is None
    run = ap.CoinRun("SOL", T0, 1, p)
    assert run.on_bar(up, ap.bar_index(up, T0), F_OK)["action"] == "end" and "ya ha ocurrido" in run.end_why
    assert ap.pre_min(p) == 250 and ap.pre_min({}) == ap.PRE_MIN


def test_limites_diarios_globales():
    assert ap.day_limits(5, 9.0, 0.5, {}) is None             # v1: sin límites
    assert "Tope diario de 3 entradas" in ap.day_limits(3, 0.5, 0.5, {"max_day_entries": 3})
    assert ap.day_limits(2, 1.0, 0.5, {"max_day_entries": 3, "max_day_risk_pct": 1.5}) is None
    why = ap.day_limits(2, 1.2, 0.5, {"max_day_risk_pct": 1.5})
    assert why.startswith("Presupuesto de riesgo del día agotado: 1,20 % comprometido + 0,50 % > 1,50 %")


def test_dynamic_stop_v2_como_el_paper():
    assert ap.dynamic_stop(1, 100.0, 1.0, None, 101.2, 99.0, False, be_r=1.5, be_pct=0.26) == (99.0, False, None)
    stop, be, k = ap.dynamic_stop(1, 100.0, 1.0, None, 101.5, 99.0, False, be_r=1.5, be_pct=0.26)
    assert (be, k) == (True, "be") and stop == pytest.approx(100.26)
    stop, be, k = ap.dynamic_stop(-1, 100.0, 1.0, None, 98.0, 101.0, False, be_r=2, be_pct=0.26)
    assert (be, k) == (True, "be") and stop == pytest.approx(99.74)
    assert ap.dynamic_stop(1, 100.0, 1.0, None, 105.0, 99.0, False, be_r=None) == (99.0, False, None)
    assert ap.dynamic_stop(1, 100.0, 1.0, 0.5, 100.8, 99.0, False, be_r=None, trail_start_r=1.0) == (99.0, False, None)
    stop, be, k = ap.dynamic_stop(1, 100.0, 1.0, 0.5, 101.2, 99.0, False, be_r=None, trail_start_r=1.0)
    assert k == "dinamico" and stop == pytest.approx(101.2 * 0.995) and not be


# ---------------------------------------------------------------- v2: simulación del plan

def test_simulate_plan_igual_que_el_backtest_con_el_plan_v1():
    from bot import backtest_aperturas as bta
    bars = [bar(T0 + k * MIN, 100 + math.sin(k / 3), 100.4 + math.sin(k / 3) + 0.2 * (k % 4),
                99.6 + math.sin(k / 3) - 0.2 * (k % 3), 100 + math.sin((k + 1) / 3)) for k in range(120)]
    for d in (1, -1):
        for stop, trail in ((1.0, 0.5), (0.4, None), (2.0, 3.0)):
            a = ap.simulate_plan(bars, 3, d, {"stopPct": stop, "trailPct": trail}, T0 + 100 * MIN, 0.001, 0.0002)
            b = bta.simulate_trade(bars, 3, d, stop, trail, T0 + 100 * MIN, 0.001, 0.0002)
            assert (a["k"], a["why"]) == (b["k"], b["why"]) and a["ret"] == pytest.approx(b["ret"])


def seq(*hlc, o=100.0):
    """Velas desde T0 con (máximo, mínimo, cierre); cada una abre en el cierre anterior."""
    out = []
    for k, (h, l, c) in enumerate(hlc):
        out.append(bar(T0 + k * MIN, o, h, l, c))
        o = c
    return out


def test_simulate_plan_objetivo_stop_parcial_y_break_even():
    end = T0 + 60 * MIN
    sim = lambda bars, d, plan: ap.simulate_plan(bars, 0, d, plan, end, 0.001, 0.0)
    r = sim(seq((100.5, 99.5, 100.2), (102.3, 100.1, 102.0)), 1, {"stopPct": 1.0, "tp2R": 2, "beR": None})
    assert r["first"] == "objetivo" and r["ret"] == pytest.approx((0.02 - 0.001 * 2.02) * 100) and r["R"] == r["ret"]
    r = sim(seq((100.5, 99.8, 100.3), (101.2, 100.2, 101.1)), -1, {"stopPct": 1.0, "tp2R": 2, "beR": None})
    assert r["first"] == "stop" and r["why"] == "inicial" and r["ret"] == pytest.approx((-0.01 - 0.001 * 2.01) * 100)
    r = sim(seq((102.5, 98.5, 100)), 1, {"stopPct": 1.0, "tp2R": 2, "beR": None})   # toca los dos: cuenta el stop
    assert r["first"] == "stop"
    r = sim(seq((101.1, 99.9, 101.0), (101.0, 98.8, 99.0)), 1,
            {"stopPct": 1.0, "tp1R": 1, "tp1Frac": 0.5, "tp2R": 3, "beR": None})   # mitad en +1 R y el resto al stop
    assert r["first"] == "stop" and r["ret"] == pytest.approx(-0.001 * (1 + 0.5 * 1.01 + 0.5 * 0.99) * 100)
    r = sim(seq((101.6, 99.9, 101.5), (101.5, 100.1, 100.2)), 1, {"stopPct": 1.0, "tp2R": 3, "beR": 1.5})
    assert r["why"] == "be" and r["ret"] == pytest.approx((0.0026 - 0.001 * 2.0026) * 100) and r["ret"] > 0
    r = sim(seq(*[(100.3, 99.7, 100.1)] * 70), 1, {"stopPct": 1.0, "tp2R": 2, "beR": None})
    assert r["first"] == "ninguno" and r["why"] == "tiempo" and r["t"] == end


# ---------------------------------------------------------------- v2: puerta de probabilidades

def cases(hits, stops, neither=0, tR=2.0, win=1.8, loss=-1.2):
    return ([{"first": "objetivo", "R": win, "tR": tR}] * hits + [{"first": "stop", "R": loss, "tR": tR}] * stops
            + [{"first": "ninguno", "R": 0.1, "tR": tR}] * neither)


def test_veredicto_de_la_puerta():
    g = ap.gate_verdict(cases(15, 15), {}, 60)               # 50 % frente a 33 %: +16,7 puntos
    assert g["verdict"] == "claramente_a_favor" and g["ok"] and g["n"] == 30
    assert g["breakeven"] == pytest.approx(1 / 3, abs=1e-4)
    assert g["text"].startswith("Claramente a favor: objetivo antes que stop en 50 % de 30 rupturas pasadas "
                                "(punto muerto 33 %")
    g = ap.gate_verdict(cases(11, 19), {})                   # 36,7 % frente a 33,3 %: moneda al aire
    assert g["verdict"] == "moneda_al_aire" and not g["ok"] and g["text"].startswith("Moneda al aire")
    g = ap.gate_verdict(cases(12, 18), {})                   # se inclina, pero el R medio con costes es 0
    assert g["verdict"] == "a_favor" and g["avg_R"] == pytest.approx(0) and not g["ok"] and "no se entra" in g["text"]
    g = ap.gate_verdict(cases(5, 25), {})
    assert g["verdict"] == "claramente_en_contra" and not g["ok"]
    g = ap.gate_verdict(cases(6, 4), {})                     # pocos casos: aunque gane, no se cumple
    assert g["verdict"] == "pocos_casos" and not g["ok"] and "mínimo" in g["text"]
    assert ap.gate_verdict([], {}, 60)["verdict"] == "pocos_casos"
    assert ap.gate_verdict(cases(15, 15, tR=None), {})["verdict"] == "sin_objetivo"
    g = ap.gate_verdict(cases(15, 15, neither=25), {})
    assert g["ok"] and "no tocó ni objetivo ni stop" in g["text"]
    assert not ap.gate_verdict(cases(15, 15), {"gate_pp": 20})["ok"]


def past_session(po, win, hold=4, pre=250):
    """Sesión pasada: pre min planos, rango de 5 min, ruptura al alza con volumen, `hold` velas por encima, la vela de
    entrada y luego el objetivo (win) o el stop; plana hasta las 4 h."""
    bs = flat(po - pre * MIN, pre)                          # 4 h previas: MOVING y ATR de 5 min
    bs += [bar(po + k * MIN, 100, 100.5, 99.5, 100) for k in range(5)]
    bs.append(bar(po + 5 * MIN, 100.4, 101.2, 100.0, 101.0, 60.0, 50.0))
    bs += [bar(po + (6 + k) * MIN, 101.0, 101.3, 100.9, 101.1) for k in range(hold)]
    t = po + (6 + hold) * MIN
    bs.append(bar(t, 101.1, 101.4, 101.0, 101.2))
    bs.append(bar(t + MIN, 101.2, 104.0, 101.1, 103.8) if win else bar(t + MIN, 101.1, 101.2, 99.0, 99.2))
    return bs + flat(t + 2 * MIN, 240 - (8 + hold), 103.8 if win else 99.2)


GATE_P = {"range_min": 5, **V2_STOP, "target_r": 2.0, "be_r": None, "trail_atr": 0, "max_reentries": 0,
          "gate": True, "gate_sessions": 25, "gate_min_n": 20}


def history_for(lead, open_ms, wins, n=30):
    """Velas de n sesiones anteriores de la bolsa lead: ganan las de los índices en wins (0 = la más reciente)."""
    rows = []
    for k, po in enumerate(ap.prev_session_opens(lead, open_ms, n, max_days=120)):
        rows += past_session(po, k in wins)
    return sorted(rows)


def test_odds_gate_con_las_sesiones_anteriores():
    ny = int(datetime(2026, 10, 26, 13, 30, tzinfo=timezone.utc).timestamp() * 1000)
    rows = history_for("nuevayork", ny, wins={k for k in range(25) if k % 5 < 3})
    get = lambda a, b: rows[ap.bar_index(rows, a):ap.bar_index(rows, b)]
    g = ap.odds_gate("SOL", 1, ny, "nuevayork", get, None, GATE_P)
    assert g["n"] == 25 and g["hits"] == 15 and g["stops"] == 10 and g["dir"] == 1 and g["sessions"] == 25
    assert g["target_R"] == pytest.approx(1.94, abs=0.02)     # 2 R desde el cierre que confirma, menos desde la entrada
    assert g["verdict"] == "claramente_a_favor" and g["ok"] and g["avg_R"] > 0
    bad = history_for("nuevayork", ny, wins=set(range(7)))
    g = ap.odds_gate("SOL", 1, ny, "nuevayork", lambda a, b: bad[ap.bar_index(bad, a):ap.bar_index(bad, b)], None,
                     GATE_P)
    assert g["hits"] == 7 and g["verdict"] == "en_contra" and not g["ok"]
    assert ap.odds_gate("SOL", -1, ny, "nuevayork", get, None, GATE_P)["n"] == 0   # a la baja no rompió nunca
    c = ap.session_case("SOL", 1, ap.prev_session_opens("nuevayork", ny, 1)[0], rows, None, [], GATE_P)
    assert c["first"] == "objetivo" and c["R"] > 1.5          # la más reciente gana: 2 R menos costes y deslizamiento


class FakeHist:
    def __init__(self, rows):
        self.rows = rows

    def slice(self, a, b):
        return self.rows[ap.bar_index(self.rows, a):ap.bar_index(self.rows, b)]


def _v2_engine(wins, pf=None, extra=None):
    rows = history_for("londres", T0, wins)
    p = {**ap.V2, **GATE_P, "confirm_tf": 5, "rvol_min": 0.5, **(extra or {})}
    now = [T0 + 10 * MIN + 3000]
    sent, bus = [], FakeBus()
    pf = pf if pf is not None else {"positions": [], "pending": [], "closed": []}
    eng = ap.OpeningEngine(bus, None, lambda c, w: {"dir": 1 if c == "BTC" else 0, "strength": 0.6, "why": ""},
                           lambda *a: sent.append(a) or True, lambda: pf, clock=lambda: now[0], params=p,
                           history=lambda coin, months: FakeHist(rows))
    data = five_min_bars() + [bar(T0 + k * MIN, 101.1, 101.3, 100.9, 101.1) for k in range(10, 40)]

    async def fetch(coin, start, limit):
        return [b for b in data if start <= b[0] < start + limit * MIN and b[0] + MIN <= now[0]]

    eng.fetch_klines = fetch
    return eng, sent, bus, now


async def test_motor_v2_puerta_superada_y_ticket_con_las_salidas():
    eng, sent, bus, now = _v2_engine(wins=set(range(30)))
    await eng.step()
    assert len(sent) == 1
    t = sent[0][0]
    assert t["entryType"] == "Ruptura confirmada al cierre de 5 min (a mercado)"
    assert t["beR"] is None and t["trailPct"] is None and t["tp1Frac"] == 0.0 and t["trailStartR"] is None
    assert t["stopPct"] == pytest.approx(1.088, abs=1e-3)   # medio del rango (100) desde 101,1
    assert t["tp2R"] == pytest.approx(2.0, abs=1e-3)
    w = next(x for x in eng.view()["windows"] if x["id"].startswith("europa-"))
    g = w["coins"]["BTC"]["gate"]
    assert g["verdict"] == "claramente_a_favor" and g["ok"] and g["n"] == 25 and g["hits"] == 25
    assert w["coins"]["BTC"]["plan"]["ok"] and w["coins"]["ETH"]["gate"] is None
    st = bus.state["openings"]
    assert st["day"]["entries"] == 1 and st["day"]["max_entries"] == 3 and st["params"]["confirm_tf"] == 5


async def test_motor_v2_puerta_no_superada_no_entra():
    eng, sent, bus, now = _v2_engine(wins={0, 1, 2})
    await eng.step()
    run = _run(eng)
    assert not sent and run.done and run.end_why.startswith("Puerta de probabilidades: Claramente en contra")
    w = next(x for x in eng.view()["windows"] if x["id"].startswith("europa-"))
    assert w["coins"]["BTC"]["gate"]["verdict"] == "claramente_en_contra" and w["coins"]["BTC"]["state"] == "terminada"


async def test_motor_v2_sin_historia_la_puerta_no_se_cumple():
    eng, sent, bus, now = _v2_engine(wins=set())
    eng.history = lambda coin, months: None
    await eng.step()
    assert not sent and "Ninguna ruptura pasada" in _run(eng).end_why


async def test_motor_v2_tope_diario_y_rr_desde_el_precio_actual():
    old = {"story": "a-tokio-seul-2026-10-26-eth-1", "opened": T0 - 6 * 3600_000, "closed": T0 - 5 * 3600_000,
           "riskUsd": 40.0, "pnl_pct": -1.0, "reason": "Stop de pérdida"}
    pf = {"positions": [], "pending": [], "closed": [old], "equity": 10000.0}
    eng, sent, bus, now = _v2_engine(wins=set(range(30)), pf=pf, extra={"max_day_entries": 1})
    await eng.step()
    assert not sent and _run(eng).done and "Tope diario de 1 entradas" in _run(eng).end_why
    assert eng.view()["day"]["entries"] == 1 and eng.view()["day"]["risk_pct"] == pytest.approx(0.4)
    eng, sent, bus, now = _v2_engine(wins=set(range(30)))
    bus.state["market"] = {"prices": {"BTC": 102.6}}         # el precio ya corrió hacia el objetivo
    await eng.step()
    run = _run(eng)
    assert not sent and run.entries == 0 and run.rejects == 1 and "el movimiento ya ha ocurrido" in run.last_why


# ---------------------------------------------------------------- revisión: entrada, estado del día y puerta

async def test_motor_no_entra_si_el_precio_vivo_ya_cruzo_el_stop():
    now, pf = [T0 + 7 * MIN + 3000], {"positions": [], "pending": [], "closed": []}
    bus = FakeBus()
    bus.state["market"] = {"prices": {"BTC": 99.0}, "updated": {"BTC": now[0] - 2000}}   # por debajo del stop
    eng, sent = _engine(live_bars(), now, pf, bus=bus)
    await eng.step()
    run = _run(eng)
    assert not sent and run.entries == 0 and run.rejects == 1 and "ya ha cruzado el stop" in run.last_why
    bus2 = FakeBus()
    bus2.state["market"] = {"prices": {"BTC": 101.05}, "updated": {"BTC": now[0] - 2000}}
    eng, sent = _engine(live_bars(), now, pf, bus=bus2)
    await eng.step()
    t = sent[0][0]
    assert t["maxAgeMs"] == ap.MAX_FILL_DELAY_MS and "beR" not in t   # v1 (stop cercano): salidas de la v1 en el paper


async def test_motor_estado_del_dia_cambia_a_medianoche_de_madrid():
    now, pf = [T0 - 9 * 3600_000], {"positions": [], "pending": [], "closed": []}   # 26-10 a las 00:00 de Madrid
    eng, sent = _engine(live_bars(), now, pf)
    eng.day = {"date": "2026-10-25", "entries": {"a-x-1": 0.4, "a-x-2": 0.4, "a-x-3": 0.4}}
    await eng.step()
    d = eng.view()["day"]
    assert d["date"] == "2026-10-26" and d["entries"] == 0 and d["risk_pct"] == 0
    assert eng.view()["params_source"] == "manual" and eng.view()["notice"] is None


async def test_motor_puerta_con_las_4_h_previas_de_las_sesiones_recientes():
    eng, sent, bus, now = _v2_engine(wins=set(range(30)))
    calls, orig = [], eng.fetch_klines

    async def rec(coin, start, limit):
        calls.append((coin, start, limit))
        return await orig(coin, start, limit)

    eng.fetch_klines = rec
    await eng.step()
    w = next(x for k, x in eng.windows.items() if k.startswith("europa-"))
    pre = ap.pre_min(eng.p)
    assert pre == 250
    assert [(s, n) for c, s, n in calls if c == "BTC"][:len(w["prev"])] == [(po - pre * MIN, pre + 240 + 10)
                                                                           for po in w["prev"]]


def test_caso_pasado_sin_las_velas_previas_no_cuenta():
    p = {**GATE_P, "moving_pct": 3.0}                          # MOVING: hacen falta 250 min previos
    po = ap.prev_session_opens("londres", T0, 1)[0]
    assert ap.session_case("SOL", 1, po, past_session(po, True), None, [], p)["first"] == "objetivo"
    short = past_session(po, True, pre=80)
    assert not ap.pre_coverage_ok(short, po, p) and ap.session_case("SOL", 1, po, short, None, [], p) is None
    assert ap.session_case("SOL", 1, po, short, None, [], GATE_P) is not None   # sin MOVING bastan 80 min


def test_paper_detenido_no_cuenta_como_rechazo_ni_gasta_opus():
    from types import SimpleNamespace
    from bot.apertura import CoinRun
    from bot.claude_agents import Phase2
    run = CoinRun("BTC", 0, 1)
    run.state, run._back, run.entries = "en_posicion", "armada", 1
    run.on_paused(0, "paper detenido")
    assert run.rejects == 0 and run.entries == 0 and run.state == "armada" and not run.done
    dummy = SimpleNamespace(_win_count={})
    assert Phase2._room(dummy, ("w", 1), {"halted": True}) == 0
    assert Phase2._room(dummy, ("w", 1), {"limits": {"halted": True}}) == 0
    assert Phase2._room(dummy, ("w", 1), {"positions": []}) > 0


def test_motor_aplica_las_reglas_del_usuario(monkeypatch):
    from bot import config
    from bot.apertura import OpeningEngine
    monkeypatch.setattr(config, "USER_RULES", True)
    eng = OpeningEngine(None, None, lambda *a: None, lambda *a: True, lambda: {}, params={"stop_mode": "cercano"})
    p = eng.p
    assert p["stop_min_pct"] >= 1.5 and p["target_r"] >= 2 and p["min_rr"] >= 2 and p["target_pct"] is None
    assert p["be_r"] is None and not p["trail_atr"] and p["tp1_frac"] == 0 and p["stop_mode"] == "or_far"


# ---------------------------------------------------------------- modo impulso (preset del laboratorio, largos y cortos)

DAY = ap.DAY


def imp_bars(open_ms, d=1, move=0.3, n=40, px=100.0, v=30.0):
    """Velas de una apertura: se mueve move % a favor de d en los 15 primeros min y luego queda plana."""
    out = []
    for k in range(n):
        c = px * (1 + d * move / 100 * min(k + 1, 15) / 15)
        o = out[-1][4] if out else px
        out.append(bar(open_ms + k * MIN, o, max(o, c) + 0.001, min(o, c) - 0.001, c, v, v / 2))
    return out


def prior_sessions(lead="londres", open_ms=T0, v=10.0, n=5):
    return [(po, flat(po, 125, v=v)) for po in ap.prev_session_opens(lead, open_ms, 5)[:n]]


def feed_all(run, bars):
    acts = []
    for b in bars:
        act = run.feed(b)
        if act:
            acts.append(act)
        if run.done or run.state == "en_posicion":
            break
    return acts


def test_preset_impulso_es_la_prometedora_del_laboratorio():
    p, notes = ap.impulse_params()
    assert not notes and p["lab"] == ap.LAB_REF == "ap_mom:15:0,2|rvol:1,5|todos|a_favor|todas|todas|todos|t:30|3|3|sig"
    assert (p["measure_min"], p["min_move_pct"], p["entry"], p["rvol_min"]) == (15, 0.2, "t:30", 1.5)
    assert p["btc_filter"] == "a_favor" and p["vol_filter"] == "todos" and p["news_mode"] == "no_contra"
    assert p["windows"] == list(ap.LAB_WINDOWS) and p["coins"] == ["BTC", "ETH", "SOL", "XRP"]
    assert (p["sl_pct"], p["target_r"], p["max_hold_h"], p["min_net_pct"]) == (3.0, 3.0, 168, 3.0)
    assert (p["long_instrument"], p["short_instrument"]) == ("spot", "perp")
    assert p["be_r"] is None and not p["trail_atr"] and p["tp1_frac"] == 0 and p["session_max_min"] == 120
    assert ap.IMP_STATES == ("esperando_apertura", "midiendo_impulso", "esperando_entrada", "en_posicion", "terminada")
    # Variantes: dejan de reproducir el laboratorio; lo que no vale vuelve al preset con aviso.
    v, notes = ap.impulse_params({"entry": "pb", "measure_min": 30, "windows": "asia", "coins": "BTC+ETH",
                                  "rvol_min": None, "btc_filter": "no_contra"})
    assert v["lab"] is None and v["entry"] == "pb" and v["windows"] == ["tokio-seul", "china"] and not notes
    assert v["coins"] == ["BTC", "ETH"] and v["rvol_min"] is None
    bad, notes = ap.impulse_params({"entry": "t:5", "long_instrument": "cfd", "max_day_entries": 0})
    assert bad["entry"] == "t:15" and bad["long_instrument"] == "spot" and bad["max_day_entries"] == 4
    assert len(notes) == 3 and any("se entra al medir ('t:15')" in n for n in notes)
    assert ap.impulse_params({"entry": "t:x"})[0]["entry"] == "t:30"   # una entrada que no se entiende: la del preset


def test_impulso_medido_en_60_min_entra_al_medir():
    # Con measure_min 60 y la entrada del preset (t:30), se entra al medir, a +60 min, y todo lo dice.
    p, notes = ap.impulse_params({"measure_min": 60})
    assert p["entry"] == "t:60" and p["measure_min"] == 60 and p["lab"] is None
    assert notes == ["entry 't:30' es antes de terminar la medida de 60 min: se entra al medir ('t:60')"]
    assert ap.impulse_entry_type(p) == "A +60 min de la apertura, a favor del impulso (a mercado)"
    run = ap.ImpulseRun("BTC", T0, p, {"btc_dir": 1, "atr_pct": None, "prev_react": None}, None, prior_sessions())
    bars = imp_bars(T0, 1, 0.3, n=70)
    assert feed_all(run, bars[:59]) == [] and run.state == "midiendo_impulso"
    [act] = feed_all(run, bars[59:])
    assert act["t"] == T0 + 60 * MIN and act["levels"]["hora_entrada"] == T0 + 60 * MIN
    assert act["rvol"] == pytest.approx(3.0) and act["tags"]["medida_min"] == 60


def test_impulso_ventanas_y_monedas_sueltas_o_separadas_por_comas():
    p, notes = ap.impulse_params({"windows": "china", "coins": "ETH,SOL"})
    assert p["windows"] == ["china"] and p["coins"] == ["ETH", "SOL"] and not notes
    p, notes = ap.impulse_params({"windows": "europa, tokio-seul", "coins": "xrp"})
    assert p["windows"] == ["tokio-seul", "europa"] and p["coins"] == ["XRP"] and not notes
    p, notes = ap.impulse_params({"windows": ["europa", "tokyo", "america"], "coins": ["BTC", "DOGE"]})
    assert p["windows"] == ["europa", "saopaulo", "nuevayork"] and p["coins"] == ["BTC"]
    assert len(notes) == 2 and "se descarta 'tokyo'" in notes[0] and "se descarta 'DOGE'" in notes[1]
    p, notes = ap.impulse_params({"windows": "londres", "coins": 3})   # nada válido: el preset, con aviso
    assert p["windows"] == list(ap.LAB_WINDOWS) and p["coins"] == ["BTC", "ETH", "SOL", "XRP"]
    assert any("'londres'" in n for n in notes) and any(n.startswith("coins = 3 no vale") for n in notes)


def test_reglas_del_usuario_suben_stop_y_objetivo_en_impulso(monkeypatch):
    from bot import config
    monkeypatch.setattr(config, "USER_RULES", True)
    p, notes = ap.impulse_params({"sl_pct": 1.0, "target_r": 1.5})
    assert p["sl_pct"] == 1.5 and p["target_r"] == 2.0
    assert notes == ["sl_pct 1,00 % sube a 1,50 % por la regla del usuario",
                     "target_r 1,50 R sube a 2,00 R por la regla del usuario"]
    assert ap.impulse_params({"sl_pct": 2.5, "target_r": 2.5})[1] == []


def test_rvol_del_laboratorio_y_miercoles_de_ceniza():
    assert ap.lab_rvol(30.0, [10.0, 20.0, 0.0, None, 40.0]) == pytest.approx(1.5)   # mediana de 10, 20 y 40
    assert ap.lab_rvol(30.0, [10.0, 20.0]) == pytest.approx(2.0)
    assert ap.lab_rvol(30.0, [10.0, 0.0]) is None and ap.lab_rvol(30.0, []) is None
    assert ap.special_session({"id": "saopaulo", "day": "2026-02-18"})
    assert not ap.special_session({"id": "saopaulo", "day": "2026-02-19"})
    assert not ap.special_session({"id": "nuevayork", "day": "2026-02-18"})


def test_impulso_t30_sigue_el_movimiento_con_volumen_y_btc_a_favor():
    p, _ = ap.impulse_params()
    run = ap.ImpulseRun("BTC", T0, p, {"btc_dir": 1, "atr_pct": None, "prev_react": 0.4}, None, prior_sessions())
    bars = imp_bars(T0, 1, 0.3)
    assert feed_all(run, bars[:15]) == [] and run.state == "esperando_entrada" and run.step == "hora"
    assert run.mom == pytest.approx(0.3) and run.dir == 1 and "entro a +30 min" in run.last_why
    [act] = feed_all(run, bars[15:])
    assert act["action"] == "enter" and act["dir"] == 1 and act["t"] == T0 + 30 * MIN
    assert act["rvol"] == pytest.approx(3.0) and run.state == "en_posicion" and run.step is None
    tags = act["tags"]
    assert tags["lado"] == "largo" and tags["instrumento"] == "spot" and tags["btc"] == "a_favor"
    assert tags["noticia"] == "neutral" and tags["apertura_anterior"] == "a_favor" and tags["rvol"] == pytest.approx(3)
    assert act["levels"]["hora_entrada"] == T0 + 30 * MIN and act["levels"]["rvol"] == pytest.approx(3)
    assert ap.impulse_strategy(act["dir"], tags) == "impulso-largo-sin-noticia"
    run.on_exit(T0 + ap.H, 2.0, "Objetivo")
    assert run.done and run.results == [2.0]


def test_impulso_no_entra_sin_volumen_contra_btc_ni_con_impulso_pequeno():
    p, _ = ap.impulse_params()
    ctx = {"btc_dir": 1, "atr_pct": None, "prev_react": None}
    run = ap.ImpulseRun("BTC", T0, p, ctx, None, prior_sessions(v=25.0))   # rvol 30 / 25 = 1,2
    assert feed_all(run, imp_bars(T0, 1, 0.3)) == [{"action": "end", "why": run.end_why}]
    assert run.done and "volumen relativo desde la apertura (1,20) no llega a 1,50" in run.end_why
    run = ap.ImpulseRun("BTC", T0, p, ctx, None, prior_sessions(n=1))      # una sola sesión de referencia
    feed_all(run, imp_bars(T0, 1, 0.3))
    assert run.done and "sin volumen de referencia" in run.end_why
    run = ap.ImpulseRun("ETH", T0, p, ctx, None, prior_sessions())         # cae con BTC en tendencia alcista
    feed_all(run, imp_bars(T0, -1, 0.3))
    assert run.done and run.dir == -1 and "tendencia diaria de BTC no acompaña" in run.end_why
    run = ap.ImpulseRun("BTC", T0, p, ctx, None, prior_sessions())
    feed_all(run, imp_bars(T0, 1, 0.15))
    assert run.done and "no llega a ±0,20 %" in run.end_why
    run = ap.ImpulseRun("BTC", T0, p, ctx, None, prior_sessions())         # sin la vela de la apertura
    feed_all(run, imp_bars(T0, 1, 0.3)[1:])
    assert run.done and "Falta la vela de la apertura" in run.end_why


def test_impulso_corto_en_perpetuo_y_noticias():
    p, _ = ap.impulse_params({"btc_filter": "ninguno"})
    red = {"dir": -1, "strength": 0.6}
    run = ap.ImpulseRun("ETH", T0, p, {"btc_dir": 1}, red, prior_sessions())
    [act] = feed_all(run, imp_bars(T0, -1, 0.4))
    assert act["dir"] == -1 and act["tags"]["lado"] == "corto" and act["tags"]["instrumento"] == "perp"
    assert act["tags"]["noticia"] == "a_favor" and act["tags"]["btc"] == "en_contra"
    assert ap.impulse_strategy(-1, act["tags"]) == "impulso-corto-noticia-a-favor"
    assert ap.impulse_instrument(-1, p) == "perp" and ap.impulse_instrument(1, p) == "spot"
    green = {"dir": 1, "strength": 0.6}
    run = ap.ImpulseRun("ETH", T0, p, {}, green, prior_sessions())            # no_contra: noticias en contra
    feed_all(run, imp_bars(T0, -1, 0.4))
    assert run.done and "noticias van claramente en contra" in run.end_why
    lab = ap.impulse_params({"btc_filter": "ninguno", "news_mode": "etiqueta"})[0]
    run = ap.ImpulseRun("ETH", T0, lab, {}, green, prior_sessions())          # etiqueta: entra y lo apunta
    [act] = feed_all(run, imp_bars(T0, -1, 0.4))
    assert act["tags"]["noticia"] == "en_contra"
    assert ap.impulse_strategy(-1, act["tags"]) == "impulso-corto-noticia-en-contra"
    only = ap.impulse_params({"btc_filter": "ninguno", "news_mode": "a_favor"})[0]
    run = ap.ImpulseRun("ETH", T0, only, {}, {"dir": 0, "strength": 0.1}, prior_sessions())
    feed_all(run, imp_bars(T0, -1, 0.4))
    assert run.done and "se exige noticia a favor" in run.end_why


def test_impulso_reanudacion_y_ruptura_como_el_laboratorio():
    p = ap.impulse_params({"entry": "pb", "rvol_min": None})[0]
    run = ap.ImpulseRun("BTC", T0, p, {"btc_dir": 1})
    bars = imp_bars(T0, 1, 0.6, n=15)               # máximo ~100,6 a los 15 min: el retroceso a la mitad, ~100,3
    bars.append(bar(T0 + 15 * MIN, 100.6, 100.6, 100.25, 100.3))
    bars.append(bar(T0 + 16 * MIN, 100.3, 100.7, 100.3, 100.65))
    assert feed_all(run, bars[:15]) == [] and run.step == "retroceso"
    assert run.levels()["retroceso"] == pytest.approx(100.3, abs=0.01)
    assert feed_all(run, bars[15:16]) == [] and run.step == "reanudacion"
    [act] = feed_all(run, bars[16:])
    assert act["t"] == T0 + 17 * MIN and act["rvol"] is None
    p = ap.impulse_params({"entry": "brk", "rvol_min": None})[0]
    run = ap.ImpulseRun("BTC", T0, p, {"btc_dir": 1})
    bars = imp_bars(T0, 1, 0.6, n=15)
    hi, lo = max(b[2] for b in bars), min(b[3] for b in bars)
    assert feed_all(run, bars) == [] and run.step == "ruptura"
    assert run.level == pytest.approx(hi + max(100 * 2 / 1e4, 0.05 * (hi - lo)))
    bars = [bar(T0 + 15 * MIN, 100.6, 100.62, 100.5, 100.6), bar(T0 + 16 * MIN, 100.6, 100.8, 100.6, 100.75)]
    [act] = feed_all(run, bars)
    assert act["t"] == T0 + 17 * MIN


def test_plan_impulso_horquilla_y_3_por_ciento_neto(monkeypatch):
    from bot import config
    monkeypatch.setattr(config, "USER_RULES", True)
    p = ap.impulse_params()[0]
    pl = ap.impulse_plan(1, 100.0, p)
    assert pl["ok"] and pl["stop_px"] == pytest.approx(97) and pl["target_px"] == pytest.approx(109)
    assert pl["tp2R"] == pl["tp1R"] == 3 and pl["tp1Frac"] == 0 and pl["beR"] is None and pl["trailPct"] is None
    assert pl["net_pct"] == pytest.approx(9 - 0.24) and pl["instrument"] == "spot"
    small = ap.impulse_params({"sl_pct": 1.5, "target_r": 2.0})[0]   # +3 % bruto: 2,76 % neto, no llega
    pl = ap.impulse_plan(-1, 100.0, small, 0.0, "perp")
    assert not pl["ok"] and "quedan 2,76 % netos < 3,00 %" in pl["why"] and pl["target_px"] == pytest.approx(97)
    # Funding esperado en la duración media (65 h = 8,125 liquidaciones): el largo en perpetuo paga la tasa positiva.
    pl = ap.impulse_plan(1, 100.0, p, 1.0, "perp")
    assert pl["funding_pct"] == pytest.approx(65 / 8) and not pl["ok"] and "funding esperado" in pl["why"]
    assert ap.impulse_plan(1, 100.0, p, 0.7, "perp")["ok"]                  # 9 - 0,24 - 5,69 = 3,07 %
    assert ap.impulse_plan(-1, 100.0, p, 0.7, "perp")["funding_pct"] == 0   # el corto lo cobraría: no se cuenta
    assert ap.impulse_plan(1, 100.0, p, 0.7, "spot")["funding_pct"] == 0     # al contado no hay funding
    monkeypatch.setattr(config, "USER_RULES", False)
    assert ap.impulse_plan(1, 100.0, small)["ok"]   # sin las reglas, solo objetivo ≥ 3 × coste


class ImpData:
    """Velas de mentira para el motor en modo impulso: la sesión de cada moneda, sus sesiones anteriores (volumen 10) y
    cierres diarios de BTC al alza."""

    def __init__(self, now, moves, open_ms=T0, lead="londres"):
        self.now = now
        self.m1 = {}
        for c, (d, mv) in moves.items():
            rows = imp_bars(open_ms, d, mv, n=130)
            for po, bs in prior_sessions(lead, open_ms):
                rows += bs
            self.m1[c] = sorted(rows)
        self.calls = []

    async def fetch_interval(self, coin, interval, start, limit):
        self.calls.append((coin, interval, start, limit))
        if interval == "1d":
            d0 = T0 // DAY - 100
            return [bar((d0 + k) * DAY, 100 + k, 101 + k, 99 + k, 100 + k) for k in range(100)]
        if interval == "1h":
            return []
        return [b for b in self.m1.get(coin, []) if start <= b[0] < start + limit * MIN and b[0] + MIN <= self.now[0]]


def imp_engine(now, moves, params=None, pf=None, sent=None, bus=None, submit=None):
    data = ImpData(now, moves)
    pf = {"positions": [], "pending": [], "closed": []} if pf is None else pf
    sent = [] if sent is None else sent
    eng = ap.OpeningEngine(bus or FakeBus(), None, lambda c, w: {"dir": 0, "strength": 0.0, "why": "sin noticias"},
                           submit or (lambda *a: sent.append(a) or True), lambda: pf, clock=lambda: now[0],
                           params={"estrategia": "impulso", "windows": ["europa"], "coins": list(moves), **(params or {})})
    eng.fetch_interval = data.fetch_interval
    return eng, sent, data


def _wimp(eng):
    return next(w for w in eng.view()["windows"] if w["id"].startswith("europa-"))


def _rimp(eng, coin):
    return eng.windows[next(k for k in eng.windows if k.startswith("europa-"))]["runs"][coin]


async def test_motor_impulso_mide_espera_y_entra_a_los_30_min(monkeypatch):
    from bot import config
    monkeypatch.setattr(config, "USER_RULES", True)
    now = [T0 - 10 * MIN]
    bus = FakeBus()
    bus.state["market"] = {"prices": {"BTC": 100.31}, "updated": {}, "flow": {"BTC": {"funding": 0.01}}}
    eng, sent, _ = imp_engine(now, {"BTC": (1, 0.3)}, bus=bus)
    await eng.step()
    st = eng.view()
    assert st["strategy"] == "impulso" and st["states"] == list(ap.IMP_STATES) and st["params_source"] == "manual"
    w = _wimp(eng)
    assert w["state"] == "esperando_apertura" and w["coins"]["BTC"]["state"] == "esperando_apertura"
    now[0] = T0 + 16 * MIN + 3000
    await eng.step()
    x = _wimp(eng)["coins"]["BTC"]
    assert x["state"] == "esperando_entrada" and x["step"] == "hora" and x["signal"] == 1 and x["instrument"] == "spot"
    assert x["tags"]["btc"] == "a_favor" and x["levels"]["impulso_pct"] == pytest.approx(0.3) and x["range"]
    assert _wimp(eng)["state"] == "esperando_entrada" and not sent
    now[0] = T0 + 30 * MIN + 3000
    await eng.step()
    [(t, story, hl, strat, win)] = sent
    assert story == "a-europa-2026-10-26-btc-1" and strat == "impulso-largo-sin-noticia" and win == "Apertura Europa"
    assert t["timing"] == "now" and t["side"] == "long" and t["instrument"] == "spot" and t["stopPct"] == 3.0
    assert t["tp2R"] == t["tp1R"] == 3.0 and t["tp1Frac"] == 0 and t["beR"] is None and t["trailPct"] is None
    assert t["winEnd"] == now[0] + 168 * ap.H and t["maxAgeMs"] == ap.MAX_FILL_DELAY_MS and t["risk"] == 0.5
    assert t["tags"]["rvol"] == pytest.approx(3.0) and t["strategy"] == strat and "(contado, paper)" in hl
    ev = next(m for m in bus.msgs if m["type"] == "event")
    assert ev["kind"] == "strategy" and ev["patch"]["tags"]["lado"] == "largo" and ev["patch"]["strat"]["tags"]
    assert ev["title"] == "Compra BTC · Impulso de apertura (paper)"
    x = _wimp(eng)["coins"]["BTC"]
    assert _wimp(eng)["state"] == "en_posicion" and x["state"] == "en_posicion" and x["rvol"] == pytest.approx(3.0)
    assert x["why"].startswith("En posición (paper): compra al contado a 100,31")
    assert x["plan"]["ok"] and x["plan"]["entry"] == 100.31 and x["plan"]["net_pct"] == pytest.approx(8.76)


async def test_motor_impulso_corto_en_perpetuo_y_tope_diario(monkeypatch):
    from bot import config
    monkeypatch.setattr(config, "USER_RULES", True)
    now = [T0 + 30 * MIN + 3000]
    eng, sent, _ = imp_engine(now, {"BTC": (1, 0.3), "ETH": (-1, 0.5)}, {"btc_filter": "ninguno", "max_day_entries": 1})
    await eng.step()
    assert len(sent) == 1 and sent[0][0]["coin"] == "BTC"   # BTC entra primero; ETH topa con el límite del día
    eth = _rimp(eng, "ETH")
    assert eth.done and "Tope diario de 1 entradas" in eth.end_why
    now2 = [T0 + 30 * MIN + 3000]
    eng, sent, _ = imp_engine(now2, {"ETH": (-1, 0.5)}, {"btc_filter": "ninguno"})
    await eng.step()
    [(t, _, hl, strat, _)] = sent
    assert t["side"] == "short" and t["instrument"] == "perp" and strat == "impulso-corto-sin-noticia" and "perpetuo" in hl


async def test_motor_impulso_una_posicion_por_moneda_y_sin_perseguir(monkeypatch):
    from bot import config
    monkeypatch.setattr(config, "USER_RULES", True)
    now = [T0 + 30 * MIN + 3000]
    pf = {"positions": [{"story": "a-china-2026-10-26-btc-1", "coin": "BTC", "side": "long"}], "pending": [],
          "closed": []}
    eng, sent, _ = imp_engine(now, {"BTC": (1, 0.3)}, pf=pf)
    await eng.step()
    run = _rimp(eng, "BTC")
    assert not sent and run.done and "ya hay una posición abierta en BTC" in run.end_why


async def test_motor_impulso_con_el_paper_real_cobra_funding_en_el_corto(tmp_path, monkeypatch):
    from bot import config, paper
    from bot.bus import Bus
    from bot.store import Store
    for k, v in {"USER_RULES": True, "APPROVAL_MODE": "auto", "PAPER_EQUITY": 10000.0, "RISK_PER_TRADE_PCT": 0.5,
                 "FEE_PCT": 0.10, "MAX_POSITIONS": 4, "MAX_DAILY_LOSS_PCT": 3.0}.items():
        monkeypatch.setattr(config, k, v)
    store = Store(tmp_path / "bot.db")
    bus = Bus(store)
    now = [T0 + 30 * MIN + 3000]
    bus.publish({"type": "market", "prices": {"ETH": 99.5}, "spread": {"ETH": 0.0}, "flow": {"ETH": {"funding": 0.01}}})
    broker = paper.PaperBroker(bus, store, lambda: bus.state.get("market"), clock=lambda: now[0], slip_bps=lambda: 0.0)
    eng, _, _ = imp_engine(now, {"ETH": (-1, 0.5)}, {"btc_filter": "ninguno"}, bus=bus, submit=broker.submit)
    eng.get_portfolio = broker.portfolio
    await eng.step()
    [p] = broker.portfolio()["positions"]
    assert p["side"] == "short" and p["instrument"] == "perp" and p["strategy"] == "impulso-corto-sin-noticia"
    assert p["tags"]["lado"] == "corto" and p["sl"] == pytest.approx(99.5 * 1.03)
    assert p["tp2"] == pytest.approx(99.5 * 0.91)
    now[0] = T0 + 8 * ap.H + 1000        # liquidación de las 16:00 UTC: el corto cobra la tasa positiva
    broker.step()
    pos = broker.positions[p["story"]]
    assert pos["funding_n"] == 1 and pos["funding"] == pytest.approx(pos["qty"] * 99.5 * 0.01 / 100)
    await eng.step()
    assert _rimp(eng, "ETH").state == "en_posicion"
    store.close()


async def test_motor_impulso_reiniciado_con_la_posicion_abierta_la_recupera(tmp_path, monkeypatch):
    from bot import config, paper
    from bot.bus import Bus
    from bot.store import Store
    for k, v in {"USER_RULES": True, "APPROVAL_MODE": "auto", "PAPER_EQUITY": 10000.0, "RISK_PER_TRADE_PCT": 0.5,
                 "FEE_PCT": 0.10, "MAX_POSITIONS": 4, "MAX_DAILY_LOSS_PCT": 3.0}.items():
        monkeypatch.setattr(config, k, v)
    store = Store(tmp_path / "bot.db")
    bus = Bus(store)
    now = [T0 + 30 * MIN + 3000]
    bus.publish({"type": "market", "prices": {"ETH": 99.5}, "spread": {"ETH": 0.0}, "flow": {"ETH": {"funding": 0.01}}})
    broker = paper.PaperBroker(bus, store, lambda: bus.state.get("market"), clock=lambda: now[0], slip_bps=lambda: 0.0)
    eng, _, _ = imp_engine(now, {"ETH": (-1, 0.5)}, {"btc_filter": "ninguno"}, bus=bus, submit=broker.submit)
    eng.get_portfolio = broker.portfolio
    await eng.step()
    assert len(broker.positions) == 1
    # Reinicio del bot con la posición abierta (dentro de la sesión): sin duplicados y con lado, instrumento,
    # etiquetas y niveles de la posición del paper.
    now[0] = T0 + 45 * MIN
    eng2, _, _ = imp_engine(now, {"ETH": (-1, 0.5)}, {"btc_filter": "ninguno"}, bus=bus, submit=broker.submit)
    eng2.get_portfolio = broker.portfolio
    await eng2.step()
    run = _rimp(eng2, "ETH")
    assert len(broker.positions) == 1 and run.state == "en_posicion" and run.entries == 1 and run.dir == -1
    assert run.tags["lado"] == "corto" and run.tags["instrumento"] == "perp" and run.entry_px == pytest.approx(99.5)
    assert run.stop_px == pytest.approx(99.5 * 1.03) and run.target_px == pytest.approx(99.5 * 0.91)
    assert run.last_why.startswith("En posición (paper, recuperada tras reiniciar): venta en perpetuo a 99,50")
    x = _wimp(eng2)["coins"]["ETH"]
    assert x["signal"] == -1 and x["instrument"] == "perp" and x["tags"]["noticia"] == "neutral"
    assert x["levels"]["entrada"] == pytest.approx(99.5) and "recuperada" in x["why"] and x["state"] == "en_posicion"
    store.close()


def test_motor_en_vivo_arranca_en_impulso_con_el_preset(tmp_path, monkeypatch):
    import json
    from bot import config
    monkeypatch.setattr(config, "USER_RULES", True)
    monkeypatch.setattr(config, "BACKTEST_DIR", tmp_path)
    monkeypatch.delenv("BOT_APERTURAS_ESTRATEGIA", raising=False)
    monkeypatch.delenv("BOT_IMPULSO_PARAMS", raising=False)
    # Una recomendación válida de la v2 (aperturas.json) no pisa el preset del modo impulso.
    (tmp_path / "aperturas.json").write_text(json.dumps({"version": 2, "period": {"months": 24},
                                                         "recommended": {"range_min": 30, "target_r": 2.0}}))
    eng = ap.OpeningEngine(FakeBus(), None, lambda *a: None, lambda *a: True, lambda: {})
    assert eng.strategy == "impulso" and eng.source == "preset_impulso" and eng.p["lab"] == ap.LAB_REF
    assert eng.p["range_min"] == ap.PARAMS["range_min"] and eng.notice is None
    monkeypatch.setenv("BOT_IMPULSO_PARAMS", '{"entry": "pb", "news_mode": "etiqueta"}')
    eng = ap.OpeningEngine(FakeBus(), None, lambda *a: None, lambda *a: True, lambda: {})
    assert eng.source == "preset_impulso+entorno" and eng.p["entry"] == "pb" and eng.p["lab"] is None
    assert "BOT_IMPULSO_PARAMS" in eng.notice
    monkeypatch.setenv("BOT_APERTURAS_ESTRATEGIA", "ruptura")
    eng = ap.OpeningEngine(FakeBus(), None, lambda *a: None, lambda *a: True, lambda: {})
    assert eng.strategy == "ruptura" and eng.source == "backtest"


def test_motor_impulso_no_opera_sao_paulo_el_miercoles_de_ceniza():
    ash = int(datetime(2026, 2, 18, 14, 0, tzinfo=timezone.utc).timestamp() * 1000)
    eng = ap.OpeningEngine(FakeBus(), None, lambda *a: None, lambda *a: True, lambda: {}, clock=lambda: ash,
                           params={"estrategia": "impulso"})
    eng._schedule(ash)
    keys = set(eng.windows)
    assert "saopaulo-2026-02-18" not in keys and "nuevayork-2026-02-18" in keys and "saopaulo-2026-02-19" in keys


# ---------------------------------------------------------------- aprendizaje, fuentes vivas y evolución

def _publish_news(bus, sid, ts, d, estado="activa", peso=1.0, url="https://www.bueno.com/a", extra=()):
    """Captada (con medio y estado de la fuente) y triada de una noticia global con dirección d."""
    bus.publish({"type": "event", "kind": "captured", "agent": "hunter", "story": sid, "ts": ts - 1000, "title": sid,
                 "patch": {"headline": sid, "src": "x", "srcId": "f", "url": url, "srcState": estado, "srcWeight": peso}})
    ev = {**news_ev(ts, d, mag=80, cred=80, nivel="global", reflejo=None), "type": "event", "story": sid, "agent": "local"}
    ev["patch"]["triage"]["resumen"] = sid
    bus.publish(ev)
    bus.publish({"type": "event", "kind": "reflejo", "agent": "flow", "story": sid, "ts": ts + 500, "title": sid,
                 "patch": {"reflejo": {"estado": "confirmada", "mov_pct": 0.7, "min": 9, "ref": "media"}}})
    for e in extra:
        bus.publish({"type": "event", "agent": "hunter", "story": sid, "ts": ts + 1000, **e})


def test_senal_con_peso_del_medio_y_sin_fuentes_silenciadas(tmp_path, monkeypatch):
    from bot import aprendizaje
    from bot.bus import Bus
    from bot.store import Store
    monkeypatch.setattr(aprendizaje, "_PESOS", {"bueno.com": 1.5, "malo.com": 0.0})
    monkeypatch.setattr(aprendizaje, "_ALIAS", {})
    store = Store(tmp_path / "t.db")
    bus = Bus(store)
    now = T0 - 10 * MIN
    _publish_news(bus, "n-sil", now - 60_000, 90, "silenciada", 0.0, url="https://neutro.org/x")
    _publish_news(bus, "n-sube", now - 60_000, 90, "silenciada", 0.0, url="https://neutro.org/y",
                  extra=[{"kind": "srcstate", "patch": {"srcState": "prueba", "srcWeight": 0.5}}])
    _publish_news(bus, "n-malo", now - 60_000, 90, url="https://malo.com/x")
    src = ap.story_sources(store, ["n-sil", "n-sube", "n-malo", "n-nada"])
    assert src["n-sil"] == {"outlet": "neutro.org", "srcId": "f", "srcState": "silenciada", "srcWeight": 0.0}
    assert src["n-sube"]["srcState"] == "prueba" and src["n-sube"]["srcWeight"] == 0.5 and "n-nada" not in src
    get = ap.signal_getter(bus, clock=lambda: now, cache_s=0)
    s = get("BTC", {"markets": ["londres"], "open_ms": T0})
    # La silenciada no cuenta; la del medio que falla (peso 0) tampoco empuja; la que subió a prueba cuenta a medias
    assert "1 noticia" in s["drivers"][0]["factor"] and "n-sube" in s["drivers"][0]["factor"]
    half = s["drivers"][0]["peso"]
    _publish_news(bus, "n-bueno", now - 60_000, 90)
    s2 = get("BTC", {"markets": ["londres"], "open_ms": T0})
    assert "2 noticias" in s2["drivers"][0]["factor"] and "n-bueno" in s2["drivers"][0]["factor"]
    assert s2["drivers"][0]["peso"] > half
    # La credibilidad sale multiplicada por el peso aprendido del medio (recortada a 0-100)
    triadas = store.events_since(0, ("triaged",))
    w = {e["story"]: e for e in ap.weigh_news(triadas, ap.story_sources(store, [e["story"] for e in triadas]))}
    assert w["n-bueno"]["credibilidad_ajustada"] == 100 and w["n-malo"]["credibilidad_ajustada"] == 0
    assert w["n-sube"]["credibilidad_ajustada"] == 80 and w["n-sube"]["medio"] == "neutro.org"   # peso 1: igual
    # Sin base (FakeStore) la señal sigue como antes
    assert ap.story_sources(FakeStore([]), ["n1"]) == {}
    store.close()


def test_noticias_pesan_por_el_estado_de_su_fuente():
    now = T0
    base = news_ev(now - 60_000, 90, mag=80, cred=80, nivel="global")
    full = ap.signal_from("BTC", ["londres"], T0, [], [base], None, now)
    half = ap.signal_from("BTC", ["londres"], T0, [], [{**base, "srcWeight": 0.5}], None, now)
    assert 0 < half["drivers"][0]["peso"] < full["drivers"][0]["peso"]
    for muted in ({"srcWeight": 0.0}, {"srcState": "retirada"}, {"srcState": "silenciada", "srcWeight": 1.0}):
        assert ap.signal_from("BTC", ["londres"], T0, [], [{**base, **muted}], None, now)["drivers"] == []
    adj = ap.signal_from("BTC", ["londres"], T0, [], [{**base, "credibilidad_ajustada": 40}], None, now)
    assert 0 < adj["drivers"][0]["peso"] < full["drivers"][0]["peso"]   # credibilidad con el peso del medio


def _activa(path, params, **extra):
    from bot import evolucion
    d = {**evolucion.activa_por_defecto(), "fuente": "evolucion", "params": params, "registro": "v-prueba",
         "motivo": "Promovida en la prueba", **extra}
    evolucion.escribir_activa(path, d)
    return d


def evo_engine(tmp_path, now, moves, sent=None, bus=None, write=None):
    path = tmp_path / "estrategia_activa.json"
    if write is not None:
        _activa(path, write)
    data = ImpData(now, moves)
    sent = [] if sent is None else sent
    pf = {"positions": [], "pending": [], "closed": []}
    eng = ap.OpeningEngine(bus or FakeBus(), None, lambda c, w: {"dir": 0, "strength": 0.0, "why": "sin noticias"},
                           lambda *a: sent.append(a) or True, lambda: pf, clock=lambda: now[0], activa_path=path)
    eng.fetch_interval = data.fetch_interval
    return eng, sent, path


@pytest.fixture
def live_impulso(monkeypatch):
    from bot import config
    monkeypatch.setattr(config, "USER_RULES", True)
    monkeypatch.setenv("BOT_APERTURAS_ESTRATEGIA", "impulso")
    monkeypatch.delenv("BOT_IMPULSO_PARAMS", raising=False)


async def test_motor_opera_la_estrategia_que_aplica_la_evolucion(tmp_path, live_impulso):
    from bot import evolucion
    now = [T0 + 30 * MIN + 3000]
    params = {"windows": ["europa"], "coins": ["BTC"], "sl_pct": 2.5, "target_r": 3.0}
    eng, sent, path = evo_engine(tmp_path, now, {"BTC": (1, 0.3)}, write=params)
    activa = evolucion.leer_estrategia_activa(path)
    assert eng.source == "evolucion" and eng.p["sl_pct"] == 2.5 and eng.p["windows"] == ["europa"] and not eng.paused
    assert eng.firma == evolucion.firma_estrategia(activa) and eng.p["lab"] is None
    await eng.step()
    [(t, *_)] = sent
    assert t["stopPct"] == 2.5 and t["tags"]["estrategia_firma"] == eng.firma
    st = eng.view()
    assert st["params_source"] == "evolucion" and st["paused"] is False
    assert st["estrategia_cargada"] == {"firma": eng.firma, "ts": T0 + 30 * MIN + 3000, "pausada": False,
                                        "fuente": "evolucion", "registro": "v-prueba", "preset": None}
    assert st["params"]["preset"] is None   # ajustada: ya no es ningún preset
    # Lo que ve la evolución: el motor confirma la versión del archivo
    assert evolucion._confirmacion_motor(activa, evolucion.firma_estrategia(activa), st["estrategia_cargada"])["confirmado"]


def test_motor_ignora_un_archivo_que_rompe_las_reglas_y_firma_del_preset(tmp_path, live_impulso):
    from bot import evolucion
    now = [T0]
    eng, _, _ = evo_engine(tmp_path, now, {"BTC": (1, 0.3)}, write={"sl_pct": 1.0, "target_r": 2.0})
    assert eng.source == "preset_impulso" and eng.p["sl_pct"] == 3.0 and eng.activa is None
    assert eng.firma == evolucion.firma_estrategia(evolucion.activa_por_defecto())
    assert evolucion.firma_motor(ap.impulse_params({})[0]) == eng.firma
    eng2, _, _ = evo_engine(tmp_path / "otro", now, {"BTC": (1, 0.3)})   # sin archivo: el preset
    assert eng2.source == "preset_impulso" and eng2.firma == eng.firma and eng2.loaded["registro"] is None


async def test_motor_pausado_por_la_evolucion_mide_pero_no_entra(tmp_path, live_impulso):
    now = [T0 + 30 * MIN + 3000]
    _activa(tmp_path / "estrategia_activa.json", {"windows": ["europa"], "coins": ["BTC"]}, pausada=True,
            motivo="Pierde -0,6 R en 30 operaciones")
    eng, sent, _ = evo_engine(tmp_path, now, {"BTC": (1, 0.3)})
    assert eng.paused and eng.notice.startswith("Campeón pausado por la evolución (solo paper)")
    assert "Pierde -0,6 R" in eng.notice and eng.view()["estrategia_cargada"]["pausada"] is True
    assert eng.view()["estrategia_cargada"]["motivo"] == "Pierde -0,6 R en 30 operaciones"   # n.º 20: la web lo enseña
    await eng.step()
    run = _rimp(eng, "BTC")
    assert not sent and run.done and "pausado por la evolución" in run.end_why


async def test_motor_recarga_la_estrategia_entre_aperturas(tmp_path, live_impulso):
    now = [T0 - 5 * MIN]
    eng, sent, path = evo_engine(tmp_path, now, {"BTC": (1, 0.3)})
    first = eng.firma
    eng._schedule(now[0])
    assert any(k.startswith("europa-") for k in eng.windows)
    _activa(path, {"windows": ["nuevayork"], "coins": ["ETH"], "sl_pct": 2.5})
    eng._activa_at = float("-inf")
    eng._check_activa(now[0])   # Europa se está armando: espera
    assert eng.source == "preset_impulso" and eng.firma == first
    now[0] = T0 + 200 * MIN     # sesión de Europa terminada y Nueva York aún lejos
    eng._activa_at = float("-inf")
    eng._check_activa(now[0])
    assert eng.source == "evolucion" and eng.p["coins"] == ["ETH"] and eng.firma != first
    assert not any(not w["armed"] and not k.startswith("europa-") for k, w in eng.windows.items())
    eng._schedule(now[0])
    assert {w["id"] for w in eng.windows.values() if not w["armed"] and w["open_ms"] > now[0]} == {"nuevayork"}
    path.unlink()               # sin archivo: vuelve al preset
    eng._activa_at = float("-inf")
    eng._check_activa(now[0])
    assert eng.source == "preset_impulso" and eng.firma == first
    await eng.step()            # el paso normal no falla con la recarga


# ---------------------------------------------------------------- preset «6 diarias» y veto de noticias fiables (28-10-2026)

FIABLE = {"clave": "medio:bueno.com", "tipo": "medio", "nombre": "bueno.com", "sentido": None, "n": 40, "dias": 4,
          "acierto": 0.7, "ic90_bajo": 0.58, "estado": "fiable", "n_historias": 40, "opera": True}


@pytest.fixture
def con_fiable():
    from bot import fiables
    fiables.usar({"grupos": [FIABLE]})
    yield fiables
    fiables.usar(None)


def _seis(**over):
    return {**{k: v for k, v in ap.SEIS.items() if k not in ("estrategia", "windows", "coins")}, **over}


def test_preset_seis_sin_filtros_y_dentro_de_las_reglas(monkeypatch):
    from bot import config, evolucion
    monkeypatch.setattr(config, "USER_RULES", True)
    p, notes = ap.impulse_params(ap.SEIS)
    assert not notes and p["lab"] is None and p["estrategia"] == "impulso"
    assert (p["measure_min"], p["min_move_pct"], p["entry"], p["rvol_min"]) == (15, 0.0, "t:15", None)
    assert (p["vol_filter"], p["btc_filter"], p["news_mode"]) == ("todos", "ninguno", "fiables")
    assert (p["sl_pct"], p["target_r"], p["max_hold_h"], p["min_net_pct"]) == (1.5, 3.0, 24.0, 3.0)
    assert (p["max_day_entries"], p["max_day_risk_pct"], p["risk_pct"]) == (6, 3.0, None)
    assert p["windows"] == list(ap.LAB_WINDOWS) and p["coins"] == list(config.COINS) and p["session_max_min"] == 120
    assert ap.preset_of(p) == "seis" and ap.preset_of(ap.impulse_params({})[0]) == "impulso"
    assert ap.preset_of(ap.impulse_params({**ap.SEIS, "sl_pct": 2.0})[0]) is None
    assert ap.preset_info(p)["nombre"] == "6 diarias" and "veto de noticias fiables" in ap.PRESETS["seis"]["descripcion"]
    assert ap.hold_text(p) == "como mucho 24 h" and ap.hold_text({}) == "sin tope (7 días de seguridad)"
    ok, fallos = evolucion.cumple_reglas({k: v for k, v in ap.SEIS.items() if k not in ("estrategia", "lab")})
    assert ok, fallos
    bad, notes = ap.impulse_params({**ap.SEIS, "min_move_pct": -0.1})
    assert bad["min_move_pct"] == 0.2 and "min_move_pct" in notes[0]
    assert ap.impulse_plan(1, 100.0, p)["ok"] and ap.impulse_plan(1, 100.0, p)["net_pct"] == pytest.approx(4.26)


def test_seis_entra_al_medir_con_cualquier_movimiento():
    p = ap.impulse_params(ap.SEIS)[0]
    run = ap.ImpulseRun("BTC", T0, p, ctx={"atr_pct": None, "btc_dir": 1, "prev_react": None}, prior=None)
    [act] = feed_all(run, imp_bars(T0, d=-1, move=0.05))
    assert act["dir"] == -1 and act["t"] == T0 + 15 * MIN and act["rvol"] is None and act["tags"]["instrumento"] == "perp"
    assert act["tags"]["noticia_fiable"] == "ninguna" and act["tags"]["btc"] == "en_contra"   # BTC no filtra
    quieto = ap.ImpulseRun("BTC", T0, p, ctx={}, prior=None)
    feed_all(quieto, flat(T0, 30))
    assert quieto.done and "sin movimiento" in quieto.end_why


def test_veto_de_una_noticia_fiable_en_contra(con_fiable):
    p = ap.impulse_params(ap.SEIS)[0]
    now = T0 + 15 * MIN
    bajista = {**news_ev(now - 30 * MIN, -60, activos=["BTC"], tipo="exchange"), "medio": "bueno.com",
               "clase": "cripto", "title": "Bitcoin: hackeo en un exchange"}
    s = ap.signal_from("BTC", ["londres"], T0, [], [bajista], None, now, p)
    [f] = s["fiables"]
    assert f["dir"] == -1 and f["grupo"] == "medio:bueno.com" and f["nombre"] == "bueno.com"
    assert "fiables" not in ap.signal_from("ETH", ["londres"], T0, [], [bajista], None, now, p)   # habla de BTC
    tags = ap.impulse_tags(1, 0.3, {}, s, p)
    assert tags["noticia_fiable"] == "en_contra" and tags["noticia_fiable_grupo"] == "bueno.com"
    why = ap.impulse_filters(1, tags, {}, p)
    assert "grupo fiable va en contra (bueno.com" in why
    assert ap.impulse_filters(-1, ap.impulse_tags(-1, -0.3, {}, s, p), {}, p) is None   # a favor: no veta
    e2 = ap.impulse_params({"btc_filter": "ninguno", "rvol_min": None})[0]   # E2 (no_contra): este veto no es suyo
    assert ap.impulse_filters(1, ap.impulse_tags(1, 0.3, {}, s, e2), {}, e2) is None
    run = ap.ImpulseRun("BTC", T0, p, ctx={}, news=s, prior=None)
    feed_all(run, imp_bars(T0, d=1, move=0.3))
    assert run.done and not run.entries and "grupo fiable va en contra" in run.end_why
    con_fiable.usar(None)   # sin grupos fiables no veta nada
    s2 = ap.signal_from("BTC", ["londres"], T0, [], [bajista], None, now, p)
    assert "fiables" not in s2 and ap.impulse_tags(1, 0.3, {}, s2, p)["noticia_fiable"] == "ninguna"


async def test_motor_seis_estado_y_ticket_de_24_h(monkeypatch):
    from bot import config
    monkeypatch.setattr(config, "USER_RULES", True)
    full = ap.OpeningEngine(FakeBus(), None, lambda c, w: {"dir": 0}, lambda *a: True, lambda: {}, clock=lambda: T0,
                            params=dict(ap.SEIS))
    st = full.view()
    assert st["params"]["preset"] == "seis" and st["params"]["nombre"] == "6 diarias" and st["day"]["max_entries"] == 6
    assert "veto de noticias fiables" in st["params"]["descripcion"] and st["estrategia_cargada"]["preset"] == "seis"
    e2 = ap.OpeningEngine(FakeBus(), None, lambda c, w: {"dir": 0}, lambda *a: True, lambda: {}, clock=lambda: T0,
                          params={"estrategia": "impulso"})
    assert e2.view()["params"]["preset"] == "impulso" and e2.view()["params"]["nombre"] == "E2 · impulso"
    now = [T0 + 15 * MIN + 3000]
    eng, sent, _ = imp_engine(now, {"BTC": (1, 0.3)}, _seis())
    await eng.step()
    [(t, story, _hl, strat, _win)] = sent
    assert story == "a-europa-2026-10-26-btc-1" and strat == "impulso-largo-sin-noticia" and t["stopPct"] == 1.5
    assert t["winEnd"] == now[0] + 24 * ap.H and t["holdLabel"] == "tope de 24 h" and t["valid"].startswith("como mucho 24 h")
    assert t["tags"]["origen"] == "motor_aperturas" and t["origen"] == "motor_aperturas" and t["tags"]["preset"] is None
    assert t["tags"]["noticia_fiable"] == "ninguna" and t["tp2R"] == 3.0
    w = _wimp(eng)
    assert "cualquier movimiento" in w["why"] and "grupo fiable" in w["why"] and w["why"].endswith("como mucho 24 h")
    assert w["coins"]["BTC"]["why"].endswith("como mucho 24 h")


async def test_motor_cuenta_las_entradas_de_la_noticia_en_el_tope_del_dia():
    now = [T0 + 30 * MIN + 3000]
    hoy = ap.madrid_day_start(now[0])
    pf = {"positions": [{"story": "nf-n9-sol", "coin": "SOL", "side": "long", "opened": hoy + MIN, "riskUsd": 50.0}],
          "pending": [], "closed": [{"story": "n-fase2-ayer", "coin": "ETH", "opened": hoy - 5 * MIN, "riskUsd": 50.0}]}
    eng, sent, _ = imp_engine(now, {"BTC": (1, 0.3)}, {"btc_filter": "ninguno", "max_day_entries": 1}, pf=pf)
    await eng.step()
    assert not sent and "Tope diario de 1 entradas" in _rimp(eng, "BTC").end_why
    day = eng.view()["day"]
    assert day["entries"] == 1 and day["risk_pct"] == pytest.approx(0.5)   # la de ayer no cuenta


def test_veto_de_las_fiables_falla_en_cerrado_sin_senal():
    """Revisión del 28-10: con «6 diarias» y grupos fiables que operan, si la señal no se pudo leer (o sin el medio de
    las noticias) el veto no sabe si hay una noticia fiable en contra: no se entra. Sin grupos que operan, nada cambia."""
    from bot import fiables
    p = ap.impulse_params(ap.SEIS)[0]
    tags = ap.impulse_tags(1, 0.3, {}, {"dir": 0, "sin_dato": True}, p)
    assert tags["noticia_fiable"] == "sin_dato"
    assert ap.impulse_tags(1, 0.3, {}, {"dir": 0}, p)["noticia_fiable"] == "ninguna"
    fiables.usar(None)
    assert ap.impulse_filters(1, tags, {}, p) is None
    fiables.usar({"grupos": [{"clave": "medio:x.com", "estado": "fiable", "opera": True}]})
    try:
        assert "no se pudieron leer" in ap.impulse_filters(1, tags, {}, p)
        assert ap.impulse_filters(1, {**tags, "noticia_fiable": "ninguna"}, {}, p) is None
        e2 = ap.impulse_params(ap.IMPULSO)[0]
        assert ap.impulse_filters(1, {**tags, "btc": "a_favor"}, {}, e2) is None   # E2 no usa ese veto
    finally:
        fiables.usar(None)


def test_v2_pasa_el_precio_de_okx_a_la_escala_de_binance():
    """Hallazgo n.º 61: en el modo v2 («ruptura») el cierre y el stop salen de Binance en USDT y el precio de ahora es
    de OKX EU en USDC (unos 10 pb por debajo). Con la base del momento (mediana de los cierres frente a OKX visto a la
    vez, con 10 muestras como mínimo) el cruce del stop y la peor entrada se comparan en la misma escala."""
    bus = FakeBus()
    eng = ap.OpeningEngine(bus, None, lambda c, w: {}, lambda *a: True, lambda: {}, params={"range_min": 5},
                           clock=lambda: T0 + 30 * MIN)
    assert not eng.impulso and eng.basis_of("BTC") is None
    now = T0 + 10 * MIN + 3000
    bar = lambda t, c: [t, c, c, c, c, 1.0, c, 0.5]
    okx = [99.90, 99.91, 99.89] * 4 + [120.0]      # 120: un precio movido no cambia la mediana
    for k, px in enumerate(okx):
        t = now + k * MIN
        bus.publish({"type": "market", "prices": {"BTC": px}, "updated": {"BTC": t - 1000}})
        eng._basis_note("BTC", [bar(T0 + (9 + k) * MIN, 100.0)], t)
        eng._basis_note("BTC", [bar(T0 + (9 + k) * MIN, 100.0)], t + 1000)   # la misma vela no cuenta dos veces
        if k == 8:
            assert eng.basis_of("BTC") is None                                 # 9 muestras: aún no se usa
    t = now + len(okx) * MIN
    bus.publish({"type": "market", "prices": {"BTC": 99.0}, "updated": {"BTC": t - 60_000}})   # viejo: no cuenta
    eng._basis_note("BTC", [bar(T0 + (9 + len(okx)) * MIN, 100.0)], t)
    assert len(eng.basis["BTC"]) == 13 and eng.basis_of("BTC") == pytest.approx(100 / 99.90)
    k = eng.basis_of("BTC")
    assert eng.basis_of("BTC", T0 + 13 * 3600_000) is None                     # muestras de más de 12 h: no valen
    act = {"dir": 1, "price": 100.0, "stop_px": 99.95}
    bus.publish({"type": "market", "prices": {"BTC": 99.92}, "updated": {"BTC": t}})
    # Antes: 99,92 (USDC) < 99,95 (stop en USDT) y se daba por cruzado. En la escala de Binance es 100,02: no lo está.
    assert eng._entry_price("BTC", act, t) == pytest.approx(99.92 * k) and 99.92 * k > 100.0
    bus.publish({"type": "market", "prices": {"BTC": 99.80}, "updated": {"BTC": t}})
    assert eng._entry_price("BTC", act, t) == pytest.approx(99.80 * k)          # cruzado de verdad: entry_plan lo rechaza
    assert not ap.entry_plan(1, eng._entry_price("BTC", act, t), act, eng.p)["ok"]
    eng.basis["BTC"] = type(eng.basis["BTC"])([(i, 1.02) for i in range(12)], maxlen=ap.BASIS_N)
    assert eng.basis_of("BTC") is None                                           # un 2 % no es una base: no se usa
    bus.publish({"type": "market", "prices": {"BTC": 99.96}, "updated": {"BTC": t}})
    assert eng._entry_price("BTC", act, t) == 100.0                              # sin base: como antes
