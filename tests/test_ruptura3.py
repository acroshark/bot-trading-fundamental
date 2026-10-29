"""Ruptura V3 (preset ruptura3, 29-10-2026): las cuentas de bot/ruptura3.py, la moneda en una apertura
(apertura.RupturaRun) y el motor en vivo con el preset. Sin red: velas y funding de mentira."""
import logging
import math
import random
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bot import apertura as ap, config, evolucion as E, ruptura3 as r3
from bot.apertura import MIN

T0 = int(datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc).timestamp() * 1000)   # lunes: apertura de Europa
DAY, H4 = 86_400_000, 4 * 3_600_000
SKILL = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "crypto-regime-analyzer" / "scripts"


def P(**over):
    return ap.impulse_params({**ap.RUPTURA3, **over})[0]


def b1(t, c, v=10.0, rng=0.05, o=None):
    o = c if o is None else o
    return [t, o, max(o, c) + rng, min(o, c) - rng, c, v, v * c, v / 2]


def base_bars(until_min=15):
    """80 min planos antes de la apertura y el rango de 15 min: máximo 100,05, mínimo 99,95 (ATR de 5 min 0,1)."""
    return [b1(T0 + k * MIN, 100.0) for k in range(-80, until_min)]


def tramo(t_min, closes, v=30.0, rng=0.05):
    """Velas de 1 min desde T0 + t_min con esos cierres (cada una abre en el cierre anterior)."""
    out, prev = [], None
    for k, c in enumerate(closes):
        out.append(b1(T0 + (t_min + k) * MIN, c, v, rng, o=prev if prev is not None else c))
        prev = c
    return out


def flat_until(t_min_from, t_min_to, px=100.0):
    return [b1(T0 + k * MIN, px) for k in range(t_min_from, t_min_to)]


def ctx(zona="RISK_ON", d_dia=1, t4=None):
    t4 = {"d": 1, "adx": 30.0, "di": 1, "ext": 0.05} if t4 is None else t4
    return {"rp": {"zona": zona, "puntos": 85.0 if zona == "RISK_ON" else 20.0, "d_dia": d_dia, "t4": t4},
            "btc_dir": 0, "atr_pct": None, "prev_react": None}


def feed(run, bars, live_ok=True):
    for b in bars:
        act = run.feed(b, live_ok=live_ok)
        if act or run.done:
            return act
    return None


def codes(run):
    return [(d["fase"], d["decision"], d["motivo_codigo"]) for d in run.decs]


# ---------------------------------------------------------------- el régimen es el de la skill

def _skill(monkeypatch):
    if not SKILL.exists():
        pytest.skip("sin la skill crypto-regime-analyzer")
    monkeypatch.syspath_prepend(str(SKILL))
    import importlib
    mods = {k: importlib.import_module(f"calculators.{k}_calculator") for k in
            ("btc_trend", "alt_breadth", "funding", "drawdown_vol", "momentum_thrust")}
    monkeypatch.setattr(mods["alt_breadth"], "MIN_UNIVERSE", 3)       # las adaptaciones declaradas en el diseño
    monkeypatch.setattr(mods["momentum_thrust"], "MIN_UNIVERSE", 4)
    return mods, importlib.import_module("scorer")


def test_regimen_igual_que_la_skill_con_las_adaptaciones(monkeypatch):
    mods, scorer = _skill(monkeypatch)
    rnd = random.Random(20261029)
    for case in range(120):
        n = rnd.choice([100, 230, 380, 420, 450])
        drift = rnd.uniform(-0.004, 0.004)
        cl = {}
        for c in ("BTC", "ETH", "SOL", "XRP"):
            px, xs = 100.0 * rnd.uniform(0.5, 2), []
            for _ in range(n if c == "BTC" or rnd.random() > 0.1 else rnd.choice([40, 190])):
                px *= math.exp(drift + rnd.gauss(0, 0.03))
                xs.append(px)
            cl[c] = xs
        fu = {c: (rnd.uniform(-0.0008, 0.0009) if rnd.random() > 0.2 else None) for c in cl}
        mine = r3.regimen(cl, fu)
        k = {c: v[-r3.DIAS_REGIMEN:] for c, v in cl.items()}
        comps = {"btc_trend": mods["btc_trend"].calculate_btc_trend(k["BTC"]),
                 "alt_breadth": mods["alt_breadth"].calculate_alt_breadth({c: k[c] for c in ("ETH", "SOL", "XRP")}),
                 "funding": mods["funding"].calculate_funding_regime({f"{c}USDT": v for c, v in fu.items()}),
                 "drawdown_vol": mods["drawdown_vol"].calculate_drawdown_vol(k["BTC"]),
                 "momentum_thrust": mods["momentum_thrust"].calculate_momentum_thrust(k)}
        ref = scorer.calculate_composite_score(comps)
        assert (mine["zona"], mine["puntos"]) == (ref["zone"], ref["score"]), case
        for cid, comp in comps.items():
            assert mine["componentes"][cid]["puntos"] == comp["score"] and \
                mine["componentes"][cid]["dato"] == comp["data_available"], (case, cid)


def test_regimen_con_subidas_y_caidas_y_su_lado():
    up = {c: [100 * 1.002 ** k * (1 + 0.01 * math.sin(k * 0.7 + i)) for k in range(450)]
          for i, c in enumerate(config.COINS)}
    down = {c: [100 * 0.998 ** k * (1 + 0.01 * math.sin(k * 0.7 + i)) for k in range(450)]
            for i, c in enumerate(config.COINS)}
    a, b = r3.regimen(up, {c: 0.0001 for c in up}), r3.regimen(down, {c: 0.0001 for c in up})
    assert a["zona"] == "RISK_ON" and a["puntos"] >= 80 and b["zona"] == "RISK_OFF" and b["puntos"] < 40
    assert not a["componentes"]["dominance"] if "dominance" in a["componentes"] else True   # sin histórico: no está
    assert a["peso_disponible"] == pytest.approx(0.85)                                    # su peso se reparte
    assert r3.regimen({"BTC": up["BTC"][:100]}, {})["zona"] == "UNKNOWN"                   # con poco, sin datos
    assert (r3.lado_regimen("RISK_ON"), r3.lado_regimen("RISK_OFF"), r3.lado_regimen("NEUTRAL"),
            r3.lado_regimen("UNKNOWN")) == (1, -1, 0, 0)


def test_funding_de_8_h_con_su_periodo_y_solo_lo_liquidado():
    h = 3_600_000
    rows = [[T0 - 24 * h, 0.0003], [T0 - 16 * h, 0.0001], [T0 - 8 * h, 0.0002], [T0 + 1, 0.9]]
    assert r3.funding_8h(rows, T0) == pytest.approx(0.0002)                # la de después no cuenta
    four = [[T0 - 8 * h, 0.0001], [T0 - 4 * h, 0.00005]]
    assert r3.funding_8h(four, T0) == pytest.approx(0.0001)                # periodo de 4 h: × 2
    assert r3.funding_8h(rows[:1], T0) is None and r3.funding_8h([], T0) is None
    bars = [[T0 - k * DAY, 1, 1, 1, 100 + k] for k in range(3)]
    assert r3.cierres_cerrados(bars, T0) == [102, 101] and r3.cierres_cerrados(bars, T0 + DAY) == [102, 101, 100]


def test_tendencias_diaria_y_de_4_h():
    up = [100 + k for k in range(60)]
    assert r3.tendencia_diaria(up) == 1 and r3.tendencia_diaria(up[::-1]) == -1 and r3.tendencia_diaria(up[:49]) == 0
    assert r3.tendencia_diaria([100.0] * 60) == 0
    e = r3.ema_sembrada([1.0] * 5 + [2.0], 5)
    assert math.isnan(e[3]) and e[4] == 1.0 and e[5] == pytest.approx(1 + 2 / 6)
    n = 1200
    bars = [[T0 - (n - k) * H4, 0, 100 * 1.001 ** k * 1.002, 100 * 1.001 ** k * 0.998, 100 * 1.001 ** k] for k in range(n)]
    t4 = r3.tendencia_4h(bars, T0)
    assert t4["d"] == 1 and t4["di"] == 1 and t4["adx"] > 25 and t4["ext"] > 0
    # Las velas que aún no han cerrado o son de después no cuentan; con 1.000 da lo mismo que con la serie entera
    fut = bars + [[T0 + k * H4, 0, 1.0, 0.5, 0.6] for k in range(5)]
    assert r3.tendencia_4h(fut, T0) == t4
    last = r3.tendencia_4h(bars[-1000:], T0)
    assert last["d"] == t4["d"] and last["adx"] == pytest.approx(t4["adx"], abs=1e-9)
    assert r3.tendencia_4h(bars[-150:], T0) is None                        # hacen falta 151
    down = [[t, 0, 1e4 / h, 1e4 / lo, 1e4 / c] for t, _, h, lo, c in bars]
    assert r3.tendencia_4h(down, T0)["d"] == -1 and r3.tendencia_4h(down, T0)["di"] == -1


def test_sentido_de_la_moneda_y_sus_codigos():
    t4 = {"d": 1, "adx": 30.0, "di": 1, "ext": 0.02}
    assert r3.sentido("RISK_ON", 1, t4)[:2] == (1, "entra")
    assert r3.sentido("RISK_OFF", -1, {**t4, "d": -1, "di": -1})[:2] == (-1, "entra")
    assert r3.sentido("NEUTRAL", 1, t4)[:2] == (0, "regimen_neutral")
    assert r3.sentido("UNKNOWN", 1, t4)[:2] == (0, "regimen_neutral")
    assert r3.sentido("RISK_ON", -1, t4)[:2] == (0, "sin_tendencia")
    assert r3.sentido("RISK_ON", 1, {**t4, "d": 0})[:2] == (0, "sin_tendencia")
    assert r3.sentido("RISK_ON", 1, None)[:2] == (0, "sin_tendencia")
    assert r3.sentido("RISK_ON", 1, {**t4, "adx": 24.9})[:2] == (0, "adx_bajo")
    assert r3.sentido("RISK_ON", 1, {**t4, "di": -1})[:2] == (0, "adx_bajo")
    assert "RISK_ON" in r3.sentido("RISK_ON", 1, t4)[2]


def test_margen_stop_estructural_y_orden():
    assert r3.margen(100.05, 99.95) == pytest.approx(max(2e-4 * 100.05, 0.005))
    assert r3.margen(110, 100) == pytest.approx(0.5)
    assert r3.stop_estructural(1, 101.0, 100.05, 99.95) == pytest.approx((101 - 99.95) / 101 * 100 + 0.1)
    assert r3.stop_estructural(-1, 99.0, 100.05, 99.95) == pytest.approx((100.05 - 99) / 99 * 100 + 0.1)
    acts = [{"t": 2, "adx": 40, "ext": 0.1}, {"t": 1, "adx": 20, "ext": 0.1}, {"t": 2, "adx": 50, "ext": 0.0},
            {"t": 2, "adx": 50, "ext": 0.2}]
    assert [(a["t"], a["adx"], a["ext"]) for a in sorted(acts, key=r3.orden)] == [(1, 20, 0.1), (2, 50, 0.2),
                                                                                (2, 50, 0.0), (2, 40, 0.1)]


# ---------------------------------------------------------------- el preset

def test_preset_ruptura3_activable_y_dentro_de_las_reglas(monkeypatch):
    monkeypatch.setattr(config, "USER_RULES", True)
    p, notes = ap.impulse_params(ap.RUPTURA3)
    assert not notes and ap.preset_of(p) == "ruptura3" and p["modelo"] == "ruptura3" and p["lab"] is None
    assert (p["measure_min"], p["entry"], p["brk_tf"], p["entry_end_min"], p["session_max_min"]) == (15, "brk", 5, 240, 240)
    assert (p["sl_pct"], p["rp_stop_max_pct"], p["rp_max_apertura"], p["news_mode"]) == (2.0, 3.0, 2, "corroboradas")
    for k in ("tp1_r", "tp1_frac", "lock_r", "floor_r", "trail_r", "max_hold_h", "max_hold_active_h", "avg_hold_h",
              "max_day_entries", "max_day_risk_pct", "long_instrument", "short_instrument"):
        assert p[k] == ap.impulse_params(ap.SEIS2)[0][k], k                 # la salida A y los límites de seis2
    info = ap.preset_info(p)
    assert info["nombre"] == "Ruptura V3: rango de apertura con tendencia, volumen y régimen"
    assert "volumen ≥ 1,5" in info["descripcion"] and "como mucho 2 por apertura" in info["descripcion"]
    for name in ("impulso", "seis", "seis2"):                              # los de antes siguen siendo ellos
        assert ap.preset_of(ap.impulse_params(ap.PRESETS[name]["params"])[0]) == name
    assert ap.preset_of(P(rp_max_apertura=3)) is None and ap.preset_of(P(entry_end_min=120)) is None
    ok, fallos = E.cumple_reglas({k: v for k, v in ap.RUPTURA3.items() if k not in ("estrategia", "lab")})
    assert ok, fallos
    f3, f2 = E.firma_motor(p), E.firma_motor(ap.impulse_params(ap.SEIS2)[0])
    assert f3 != f2 and E.firma_motor(P(rp_max_apertura=1)) != f3 and E.firma_motor(P(rp_stop_max_pct=2.5)) != f3
    # la firma de las estrategias del impulso no cambia con los campos nuevos (sin el modelo ruptura3 no cuentan)
    s2 = ap.impulse_params(ap.SEIS2)[0]
    viejo = {k: v for k, v in s2.items() if k not in ("modelo", "rp_max_apertura", "rp_stop_max_pct")}
    assert E.firma_motor(viejo) == f2
    assert ap.exit_text(p, corto=True) == "stop 2-3 %, la mitad en 3R y el resto corre"
    assert ap.exit_text(p).startswith("stop 2-3 % (el otro lado del rango), la mitad sale en 3R (≥ +6 %)")
    assert ap.impulse_entry_type(p).startswith("Ruptura V3: cierre de 5 min fuera del rango de 15 min")
    hara = ap.window_plan_text(T0, p)
    assert hara.startswith("vigilará el rango de los 15 primeros min de BTC, ETH, SOL y XRP y entrará en la ruptura")
    assert "antes de las 13:00" in hara and "como mucho 2 por apertura" in hara and hara.endswith("el resto corre")
    assert ap.exit_text(ap.impulse_params(ap.SEIS2)[0], corto=True) == "stop 2 %, la mitad en 3R y el resto corre"


def test_parametros_de_la_ruptura_se_validan():
    bad, notes = ap.impulse_params({**ap.RUPTURA3, "modelo": "otro", "rp_max_apertura": 0, "rp_stop_max_pct": "x"})
    assert bad["modelo"] == "impulso" and bad["rp_max_apertura"] == 2 and bad["rp_stop_max_pct"] == 3.0 and len(notes) == 3
    p, notes = ap.impulse_params({**ap.RUPTURA3, "entry": "t:30", "brk_tf": 1})
    assert (p["entry"], p["brk_tf"]) == ("brk", 5) and "modelo ruptura3" in notes[0]
    p, notes = ap.impulse_params({**ap.RUPTURA3, "rp_stop_max_pct": 1.0})
    assert p["rp_stop_max_pct"] == p["sl_pct"] == 2.0 and "rp_stop_max_pct" in notes[0]
    assert ap.impulse_params({**ap.RUPTURA3, "rp_max_apertura": None})[0]["rp_max_apertura"] is None


@pytest.mark.parametrize("over,esperado,nota", [
    # el rango va en velas de 5 min: con 12 el volumen del rango saldría de velas a medias y la primera vela evaluada
    # se solaparía con él
    ({"measure_min": 12}, (15, 240), "measure_min = 12 no es un múltiplo de 5"),
    ({"measure_min": 30}, (30, 240), None),                                    # un múltiplo de 5 vale
    ({"entry_end_min": 17}, (15, 240), "entry_end_min = 17 no deja ninguna vela de 5 min"),
    ({"measure_min": 30, "entry_end_min": 32}, (30, 240), "entry_end_min = 32"),
    ({"measure_min": 300, "entry_end_min": 302}, (15, 240), "(y measure_min 15)"),
    ({"entry_end_min": 20}, (15, 20), None),                                    # justo una vela de 5 min tras el rango
])
def test_ruptura3_rango_y_ventana_en_velas_de_5_min(over, esperado, nota):
    p, notes = ap.impulse_params({**ap.RUPTURA3, **over})
    assert (p["measure_min"], p["entry_end_min"]) == esperado and p["session_max_min"] == esperado[1]
    if nota:
        assert any(nota in n and "modelo ruptura3" in n for n in notes), notes
    else:
        assert not notes, notes
    run = ap.RupturaRun("BTC", T0, p, ctx=ctx())
    assert run.g1 >= run.g0   # siempre queda al menos una vela de 5 min que mirar tras el rango
    # con el modelo de siempre (impulso) no se exige nada de esto
    q, notes = ap.impulse_params({**ap.SEIS2, "measure_min": 12})
    assert q["measure_min"] == 12 and not notes


@pytest.mark.parametrize("val,esperado", [(True, 2), (False, 2), (2.5, 2), ("3", 3), (3.0, 3), (float("inf"), 2)])
def test_rp_max_apertura_un_entero_de_verdad(val, esperado):
    p, notes = ap.impulse_params({**ap.RUPTURA3, "rp_max_apertura": val})
    assert p["rp_max_apertura"] == esperado
    assert bool(notes) == (val not in ("3", 3.0)) and all("rp_max_apertura" in n for n in notes)
    p, notes = ap.impulse_params({**ap.RUPTURA3, "rp_stop_max_pct": True})   # True no es «1 %»
    assert p["rp_stop_max_pct"] == 3.0 and "rp_stop_max_pct = True no vale" in notes[0]


# ---------------------------------------------------------------- la moneda en una apertura (RupturaRun)

def test_ruptura_a_favor_con_volumen_y_expansion_entra_al_cierre_de_5_min():
    run = ap.RupturaRun("BTC", T0, P(), ctx=ctx())
    act = feed(run, base_bars() + tramo(15, [100.2, 100.4, 100.6, 100.8, 101.0]))
    assert act["action"] == "enter" and act["dir"] == 1 and act["t"] == T0 + 20 * MIN and act["price"] == 101.0
    assert act["stop_pct"] == 2.0 and act["s_struct"] == pytest.approx((101 - 99.95) / 101 * 100 + 0.1)
    assert act["adx"] == 30.0 and act["ext"] == 0.05 and act["volumen_x"] == pytest.approx(3.0)
    assert "rvol" not in act and act["rango_verdadero_x"] > 1.5   # su volumen es frente al del rango, no un rvol
    assert codes(run) == [("medida", "entra", "entra")] and run.state == "en_posicion" and run.entries == 1
    tg = act["tags"]
    assert tg["modelo"] == "ruptura3" and tg["regimen"] == "RISK_ON" and tg["stop_pct"] == 2.0 and tg["lado"] == "largo"
    assert tg["min_tras_apertura"] == 20 and tg["noticia_corroborada"] == "ninguna" and tg["instrumento"] == "spot"
    assert ap.impulse_strategy(1, tg) == "impulso-ruptura3-largo-sin-noticia"
    lv = act["levels"]
    assert lv["maximo"] == 100.05 and lv["minimo"] == 99.95 and lv["ruptura"] == pytest.approx(100.05 + 0.02001)
    d = run.decs[0]["datos"]
    assert d["modelo"] == "ruptura3" and d["rango"]["velas"] == 15 and d["regimen"]["zona"] == "RISK_ON"
    # con el stop estructural más lejos que el suelo: el estructural
    run = ap.RupturaRun("BTC", T0, P(), ctx=ctx())
    act = feed(run, base_bars() + tramo(15, [100.6, 101.2, 101.6, 102.0, 102.2]))
    assert act["stop_pct"] == pytest.approx((102.2 - 99.95) / 102.2 * 100 + 0.1) and act["stop_pct"] > 2


def test_corto_en_risk_off():
    run = ap.RupturaRun("ETH", T0, P(), ctx=ctx("RISK_OFF", -1, {"d": -1, "adx": 40.0, "di": -1, "ext": 0.1}))
    act = feed(run, base_bars() + tramo(15, [99.8, 99.6, 99.4, 99.2, 99.0]))
    assert act["dir"] == -1 and act["tags"]["lado"] == "corto" and act["tags"]["instrumento"] == "perp"
    assert act["stop_pct"] == 2.0 and ap.impulse_strategy(-1, act["tags"]) == "impulso-ruptura3-corto-sin-noticia"


def test_sin_volumen_sigue_mirando_y_entra_en_la_siguiente_con_potencia():
    run = ap.RupturaRun("BTC", T0, P(), ctx=ctx())
    bars = base_bars() + tramo(15, [100.2, 100.4, 100.6, 100.8, 101.0], v=10.0)   # rompe, pero sin volumen (×1)
    assert feed(run, bars) is None and run.saw_break and not run.saw_vol and "sin volumen" in run.last_why
    act = feed(run, tramo(20, [101.0, 101.3, 101.6, 101.9, 102.2], v=40.0))
    assert act and act["t"] == T0 + 25 * MIN


def test_cierre_al_otro_lado_descarta_la_moneda():
    run = ap.RupturaRun("BTC", T0, P(), ctx=ctx())
    end = feed(run, base_bars() + tramo(15, [99.9, 99.8, 99.7, 99.6, 99.5]))
    assert end["action"] == "end" and run.done and codes(run)[-1] == ("entrada", "no_entra", "ruptura_contra")


def test_stop_mas_alla_del_3_por_ciento_no_entra():
    run = ap.RupturaRun("BTC", T0, P(), ctx=ctx())
    end = feed(run, base_bars() + tramo(15, [101.0, 102.0, 102.8, 103.2, 103.5]))
    assert end["action"] == "end" and codes(run)[-1] == ("entrada", "no_entra", "stop_lejos")
    assert run.decs[-1]["datos"]["stop_pct"] > 3


@pytest.mark.parametrize("closes,v,rng,code", [
    ([100.0] * 5, 30.0, 0.05, "sin_ruptura"),
    ([100.2, 100.4, 100.6, 100.8, 101.0], 10.0, 0.05, "sin_volumen"),
    ([100.08] * 5, 30.0, 0.0, "sin_expansion"),
])
def test_sin_disparo_al_acabar_la_ventana(closes, v, rng, code):
    run = ap.RupturaRun("BTC", T0, P(), ctx=ctx())
    bars = base_bars() + [b1(T0 + (15 + k) * MIN, c, v, rng) for k, c in enumerate(closes)]
    bars += [b1(T0 + k * MIN, closes[-1], 10.0, 0.0) for k in range(20, 240)]   # quieto hasta el final de la ventana
    end = feed(run, bars)
    assert end and end["action"] == "end" and codes(run)[-1] == ("entrada", "no_entra", code)
    assert run.g_eval == run.g1   # la última vela de 5 min que empieza antes de +240 min se miró


@pytest.mark.parametrize("c,code", [
    (ctx("NEUTRAL"), "regimen_neutral"), (ctx("UNKNOWN"), "regimen_neutral"), (ctx(d_dia=-1), "sin_tendencia"),
    (ctx(t4={"d": 0, "adx": 30.0, "di": 1, "ext": 0.0}), "sin_tendencia"),
    (ctx(t4={"d": 1, "adx": 20.0, "di": 1, "ext": 0.0}), "adx_bajo")])
def test_sin_regimen_o_tendencia_no_mira_la_moneda(c, code):
    run = ap.RupturaRun("BTC", T0, P(), ctx=c)
    end = feed(run, base_bars())
    assert end["action"] == "end" and run.dir == 0 and codes(run) == [("medida", "no_entra", code)]


def test_sin_velas_del_rango_o_vela_vieja():
    run = ap.RupturaRun("BTC", T0, P(), ctx=ctx())
    bars = [b for b in base_bars() if not T0 + 2 * MIN <= b[0] < T0 + 5 * MIN]   # faltan 3 del rango
    end = feed(run, bars + tramo(15, [100.2]))
    assert end and codes(run)[-1] == ("medida", "no_entra", "sin_velas")
    run = ap.RupturaRun("BTC", T0, P(), ctx=ctx())
    end = feed(run, base_bars() + tramo(15, [100.2, 100.4, 100.6, 100.8, 101.0]), live_ok=False)
    assert end["action"] == "end" and codes(run)[-1] == ("entrada", "no_entra", "tarde")


def test_sin_contexto_guarda_las_velas_y_decide_despues():
    run = ap.RupturaRun("BTC", T0, P(), ctx=None)
    bars = base_bars() + tramo(15, [100.2, 100.4, 100.6, 100.8, 101.0])
    assert feed(run, bars) is None and not run.decs and not run.done
    run.ctx = ctx()
    act = run.feed(b1(T0 + 20 * MIN, 101.0, 10.0))
    assert act and act["t"] == T0 + 20 * MIN   # la ruptura ya hecha, con las velas guardadas


def test_veto_de_una_noticia_corroborada_en_contra():
    run = ap.RupturaRun("BTC", T0, P(), ctx=ctx())
    act = feed(run, base_bars() + tramo(15, [100.2, 100.4, 100.6, 100.8, 101.0]))
    assert act and run.veto_noticias({"dir": 0, "strength": 0.0}) is None
    macro = {"dir": 0, "strength": 0.0, "macro": [{"story": "m1", "dir": -1, "ts": T0 - 3600_000, "titular": "Fed dura"}]}
    why = run.veto_noticias(macro)
    assert why and "corroborada" in why and "Fed dura" in why and run.tags["noticia_corroborada"] == "en_contra"
    fiable = {"dir": 0, "strength": 0.0, "fiables": [{"dir": -1, "nombre": "Grupo bueno"}]}
    assert "Grupo bueno" in run.veto_noticias(fiable)
    a_favor = {"dir": 0, "strength": 0.0, "macro": [{"dir": 1, "titular": "Fed blanda"}]}
    assert run.veto_noticias(a_favor) is None and run.tags["noticia_corroborada"] == "a_favor"


def test_las_macros_confirmadas_van_en_la_senal():
    now = T0
    news = [{"story": "m1", "ts": now - 3600_000, "title": "La Fed sube los tipos", "clase": "macro",
             "reflejo": {"estado": "confirmada"},
             "triage": {"importancia": 8, "repercusion": {"direccion": -60, "magnitud": 60}}},
            {"story": "m2", "ts": now - 3600_000, "title": "El BCE baja los tipos", "clase": "macro",
             "reflejo": {"estado": "descartada"},
             "triage": {"importancia": 8, "repercusion": {"direccion": 60, "magnitud": 60}}},
            {"story": "m3", "ts": now - 8 * 3600_000, "title": "Vieja", "clase": "macro",
             "reflejo": {"estado": "confirmada"},
             "triage": {"importancia": 8, "repercusion": {"direccion": 60, "magnitud": 60}}}]
    s = ap.signal_from("ETH", ["londres"], T0, news=news, now=now)
    assert [x["story"] for x in s["macro"]] == ["m1"] and s["macro"][0]["dir"] == -1
    assert "macro" not in ap.signal_from("ETH", ["londres"], T0, news=news[1:], now=now)


# ---------------------------------------------------------------- el motor en vivo con el preset

class FakeBus:
    def __init__(self):
        self.state, self.msgs = {}, []

    def publish(self, m):
        self.msgs.append(m)
        if m["type"] != "event":
            self.state[m["type"]] = m
        return m


class FakeDec:
    def __init__(self):
        self.rows = []

    def add(self, vid, coin, fase, decision, codigo, motivo, **kw):
        r = {"ventana_id": vid, "coin": coin, "fase": fase, "decision": decision, "motivo_codigo": codigo,
             "motivo": motivo, **kw}
        self.rows.append(r)
        return r

    def ultima(self, *a):
        return None

    def ventana(self, *a):
        return {}

    def fases(self, *a):
        return set()

    def hubo_actividad(self, *a):
        return False

    def de_story(self, *a):
        return None


class RpData:
    """Binance de mentira: cierres diarios de las 4 monedas al alza (régimen RISK_ON) o a la baja, velas de 4 h en el
    mismo sentido, la sesión de 1 min de cada moneda y el funding de las dos últimas liquidaciones."""

    def __init__(self, now, sesiones, sube=True):
        self.now, self.m1, self.sube, self.calls = now, sesiones, sube, []

    async def fetch_interval(self, coin, interval, start, limit):
        self.calls.append((coin, interval))
        g = 1.002 if self.sube else 0.998
        if interval == "1d":
            d0 = T0 // DAY - 450
            i = config.COINS.index(coin)
            return [[(d0 + k) * DAY, 1, 1, 1, 100 * g ** k * (1 + 0.01 * math.sin(k * 0.7 + i))] for k in range(450)]
        if interval == "4h":
            q = 1.001 if self.sube else 0.999
            return [[T0 - (1000 - k) * H4, 0, 100 * q ** k * 1.002, 100 * q ** k * 0.998, 100 * q ** k]
                    for k in range(1000)]
        if interval == "1h":
            return []
        return [b for b in self.m1.get(coin, []) if start <= b[0] < start + limit * MIN and b[0] + MIN <= self.now[0]]

    async def fetch_funding(self, coin, end_ms):
        return [[T0 - 16 * 3600_000, 0.0001], [T0 - 8 * 3600_000, 0.0001]]


def rp_engine(now, sesiones, pf=None, sig=None, sube=True, dec=None, **over):
    pf = {"positions": [], "pending": [], "closed": []} if pf is None else pf
    sent = []
    eng = ap.OpeningEngine(FakeBus(), None, sig or (lambda c, w: {"dir": 0, "strength": 0.0, "why": "sin noticias"}),
                           lambda *a: sent.append(a) or True, lambda: pf, clock=lambda: now[0],
                           params={**ap.RUPTURA3, "windows": ["europa"], "coins": list(sesiones), **over},
                           decisiones=dec)
    data = RpData(now, sesiones, sube)
    eng.fetch_interval, eng.fetch_funding = data.fetch_interval, data.fetch_funding
    return eng, sent, data


def _w(eng):
    return eng.windows[next(k for k in eng.windows if k.startswith("europa-"))]


def sube_rompe(v=30.0):
    return base_bars() + tramo(15, [100.2, 100.4, 100.6, 100.8, 101.0], v=v) + flat_until(20, 30, 101.0)


async def test_motor_ruptura3_contexto_entrada_y_ticket(monkeypatch):
    monkeypatch.setattr(config, "USER_RULES", True)
    now = [T0 - 20 * MIN]
    dec = FakeDec()
    eng, sent, data = rp_engine(now, {"BTC": sube_rompe()}, dec=dec)
    await eng.step()
    st = eng.view()
    w = next(x for x in st["windows"] if x["id"].startswith("europa-"))
    assert st["params"]["modelo"] == "ruptura3" and w["hara"].startswith("vigilará el rango") and not sent
    assert w["coins"]["BTC"]["why"].startswith("A las 09:00 Madrid se mira el régimen cripto")
    now[0] = T0 + 16 * MIN + 3000                                             # rango hecho: vigila la ruptura
    await eng.step()
    run = _w(eng)["runs"]["BTC"]
    assert isinstance(run, ap.RupturaRun) and run.state == "esperando_entrada" and run.dir == 1
    assert run.ctx["rp"]["zona"] == "RISK_ON" and run.ctx["rp"]["t4"]["d"] == 1 and run.ctx["rp"]["d_dia"] == 1
    assert {c for c, i in data.calls if i == "1d"} == set(config.COINS)       # el régimen, con las 4 monedas
    w = next(x for x in eng.view()["windows"] if x["id"].startswith("europa-"))
    assert w["regimen"]["zona"] == "RISK_ON" and w["coins"]["BTC"]["context"]["rp"]["zona"] == "RISK_ON"
    assert w["coins"]["BTC"]["state"] == "esperando_entrada" and w["coins"]["BTC"]["step"] == "ruptura"
    assert w["why"].startswith("Ruptura V3: vigilará el rango") and "noticia corroborada" in w["why"]
    assert w["coins"]["BTC"]["levels"]["ruptura"] == pytest.approx(100.07001) and w["coins"]["BTC"]["levels"]["adx_4h"]
    assert [(r["fase"], r["decision"], r["motivo_codigo"]) for r in dec.rows] == [("medida", "esperando", "otro"),
                                                                                  ("medida", "entra", "entra")]
    assert dec.rows[0]["motivo"].startswith("Ruptura V3: se mira el régimen cripto")
    now[0] = T0 + 20 * MIN + 3000                                             # la vela de 5 min de las 9:15 cierra
    await eng.step()
    [(t, story, hl, strat, win)] = sent
    assert story == "a-europa-2026-10-26-btc-1" and strat == "impulso-ruptura3-largo-sin-noticia"
    assert t["stopPct"] == 2.0 and (t["tp1R"], t["tp1Frac"], t["lockR"], t["lockFloorR"], t["lockTrailR"]) == \
        (3.0, 0.5, 3.0, 2.0, 2.0) and t["tp2R"] is None and t["beR"] is None and t["trailPct"] is None
    assert t["winEnd"] == now[0] + 24 * ap.H and t["winEndActive"] == now[0] + 168 * ap.H and t["instrument"] == "spot"
    assert t["tags"]["modelo"] == "ruptura3" and t["tags"]["preset"] is None and t["tags"]["regimen"] == "RISK_ON"
    assert t["entryType"].startswith("Ruptura V3") and "Ruptura V3 (régimen RISK_ON" in hl
    ev = next(m for m in eng.bus.msgs if m["type"] == "event")
    assert ev["title"] == "Compra BTC · Ruptura V3 (paper)"
    last = dec.rows[-1]
    assert (last["fase"], last["decision"], last["motivo_codigo"]) == ("entrada", "entra", "entra")
    assert last["datos"]["plan"]["stop_pct"] == 2.0 and last["datos"]["plan"]["stop_estructural_pct"] > 1
    assert last["preset"] is None and last["lado"] == "largo"
    # su volumen es el de la vela frente al del rango (×3), no un volumen relativo a sesiones anteriores (rvol)
    assert last["datos"]["volumen_x"] == pytest.approx(3.0) and last["datos"]["rango_verdadero_x"] > 1.5
    assert "rvol" not in last["datos"] and "rvol_min" not in last["datos"]


async def test_motor_ruptura3_el_preset_entero_se_llama_por_su_nombre():
    eng = ap.OpeningEngine(FakeBus(), None, lambda c, w: {"dir": 0}, lambda *a: True, lambda: {}, clock=lambda: T0 - 30 * MIN,
                           params=dict(ap.RUPTURA3))
    await eng.step()
    st = eng.view()
    assert st["params"]["preset"] == "ruptura3" and st["params"]["nombre"].startswith("Ruptura V3")
    assert st["salida_corta"] == "stop 2-3 %, la mitad en 3R y el resto corre" and st["params"]["rp_max_apertura"] == 2


async def test_motor_ruptura3_como_mucho_2_por_apertura_por_adx_y_el_hueco_pasa(monkeypatch):
    monkeypatch.setattr(config, "USER_RULES", True)
    adx = {"BTC": 30.0, "ETH": 50.0, "SOL": 40.0}
    calls = iter(["BTC", "ETH", "SOL"])

    def t4(bars, om):   # una tendencia de 4 h por moneda, en el orden en que el motor las pide
        return {"d": 1, "adx": adx[next(calls)], "di": 1, "ext": 0.01, "cierre": 1, "ema20": 1, "ema50": 1}

    monkeypatch.setattr(r3, "tendencia_4h", t4)
    now = [T0 + 20 * MIN + 3000]
    dec = FakeDec()
    eng, sent, _ = rp_engine(now, {c: sube_rompe() for c in adx}, dec=dec)
    await eng.step()
    assert [a[0]["coin"] for a in sent] == ["ETH", "SOL"]                    # las dos de mayor ADX, en ese orden
    btc = [r for r in dec.rows if r["coin"] == "BTC"][-1]
    assert (btc["decision"], btc["motivo_codigo"]) == ("no_entra", "tope_apertura")
    assert btc["datos"]["limites"]["max_apertura"] == 2
    # Con ETH ya en posición (de otra apertura), su hueco pasa a la siguiente: SOL y BTC
    calls = iter(["BTC", "ETH", "SOL"])
    pf = {"positions": [{"story": "a-china-2026-10-26-eth-1", "coin": "ETH", "side": "long"}], "pending": [], "closed": []}
    eng, sent, _ = rp_engine(now, {c: sube_rompe() for c in adx}, pf=pf, dec=FakeDec())
    await eng.step()
    assert [a[0]["coin"] for a in sent] == ["SOL", "BTC"]


async def test_motor_ruptura3_veta_una_noticia_corroborada_en_contra(monkeypatch):
    monkeypatch.setattr(config, "USER_RULES", True)
    now = [T0 + 20 * MIN + 3000]
    contra = {"dir": 0, "strength": 0.0, "why": "",
              "macro": [{"story": "m1", "dir": -1, "ts": T0 - 3600_000, "titular": "Fed dura"}]}
    dec = FakeDec()
    eng, sent, _ = rp_engine(now, {"BTC": sube_rompe(), "ETH": sube_rompe()}, dec=dec,
                             sig=lambda c, w: contra if c == "BTC" else {"dir": 0, "strength": 0.0, "why": ""})
    await eng.step()
    assert [a[0]["coin"] for a in sent] == ["ETH"]
    btc = [r for r in dec.rows if r["coin"] == "BTC"][-1]
    assert (btc["fase"], btc["decision"], btc["motivo_codigo"]) == ("entrada", "no_entra", "veto_noticia")
    assert "Fed dura" in btc["motivo"] and btc["datos"]["noticias"]["macro"][0]["story"] == "m1"


async def test_motor_ruptura3_regimen_en_contra_no_mira_ninguna(monkeypatch):
    monkeypatch.setattr(config, "USER_RULES", True)
    now = [T0 + 20 * MIN + 3000]
    dec = FakeDec()
    # Mercado a la baja (RISK_OFF), con la diaria y la de 4 h bajistas: solo mira cortos, así que la ruptura al alza es
    # una ruptura en contra y la moneda se descarta en esta apertura
    eng, sent, _ = rp_engine(now, {"BTC": sube_rompe()}, sube=False, dec=dec)
    await eng.step()
    run = _w(eng)["runs"]["BTC"]
    assert not sent and run.ctx["rp"]["zona"] == "RISK_OFF" and run.dir == -1 and run.level < 99.95
    assert run.done and [(r["fase"], r["decision"], r["motivo_codigo"]) for r in dec.rows][-1] ==         ("entrada", "no_entra", "ruptura_contra")
    # Sin régimen operable (NEUTRAL): fila «medida» con regimen_neutral al primer paso
    monkeypatch.setattr(r3, "regimen", lambda cl, fu: {"zona": "NEUTRAL", "puntos": 60.0, "peso_disponible": 0.85,
                                                        "componentes": {}})
    dec = FakeDec()
    eng, sent, _ = rp_engine(now, {"BTC": sube_rompe()}, dec=dec)
    await eng.step()
    assert not sent and [(r["fase"], r["decision"], r["motivo_codigo"]) for r in dec.rows][-1] == \
        ("medida", "no_entra", "regimen_neutral")


async def test_motor_ruptura3_sin_funding_de_binance_usa_el_guardado(monkeypatch):
    from bot import historia_extra
    now = [T0 + 16 * MIN]
    eng, _, _ = rp_engine(now, {"BTC": sube_rompe()})

    async def falla(coin, end_ms):
        raise OSError("sin red")

    eng.fetch_funding = falla
    monkeypatch.setattr(historia_extra, "load_funding", lambda sym, base=None: [[T0 - 16 * 3600_000, 0.0002],
                                                                                 [T0 - 8 * 3600_000, 0.0003],
                                                                                 [T0 + 1, 0.9]])
    await eng.step()
    assert _w(eng)["ctx"]["rp_regimen"]["funding_8h"]["BTC"] == pytest.approx(0.0003)


async def test_motor_ruptura3_reiniciado_cuenta_la_entrada_ya_hecha_en_el_tope(monkeypatch):
    """Tras reiniciar con una entrada de esta apertura ya en el paper (ETH), el tope de 2 cuenta esa: de BTC y SOL, que
    disparan después, solo entra la de mayor ADX."""
    monkeypatch.setattr(config, "USER_RULES", True)
    adx = {"BTC": 30.0, "ETH": 60.0, "SOL": 40.0}
    order = iter(["BTC", "ETH", "SOL"])   # el contexto se pide también para ETH (sigue en posición)

    def t4(bars, om):
        return {"d": 1, "adx": adx[next(order)], "di": 1, "ext": 0.01, "cierre": 1, "ema20": 1, "ema50": 1}

    monkeypatch.setattr(r3, "tendencia_4h", t4)
    now = [T0 + 20 * MIN + 3000]
    pf = {"positions": [{"story": "a-europa-2026-10-26-eth-1", "coin": "ETH", "side": "long", "entry": 100.5,
                         "sl": 98.49, "sl_initial": 98.49, "opened": T0 + 16 * MIN}], "pending": [], "closed": []}
    dec = FakeDec()
    eng, sent, _ = rp_engine(now, {"BTC": sube_rompe(), "SOL": sube_rompe()}, pf=pf, dec=dec, coins=["BTC", "ETH", "SOL"])
    await eng.step()
    eth = _w(eng)["runs"]["ETH"]
    assert eth.state == "en_posicion" and eth.entries == 1 and "recuperada tras reiniciar" in eth.last_why
    assert [a[0]["coin"] for a in sent] == ["SOL"]
    btc = [r for r in dec.rows if r["coin"] == "BTC"][-1]
    assert (btc["decision"], btc["motivo_codigo"]) == ("no_entra", "tope_apertura")


# ---------------------------------------------------------------- fallos de datos: cada moneda (y el régimen) con su espera

def _falla(eng, data, que):
    """Las velas de mentira, pero que(coin, interval) → True hace fallar esa petición (apuntada en data.fallos)."""
    base, data.fallos = data.fetch_interval, []

    async def fetch(coin, interval, start, limit):
        if que(coin, interval):
            data.fallos.append((coin, interval))
            raise OSError(f"{interval} de {coin} caídas")
        return await base(coin, interval, start, limit)

    eng.fetch_interval = fetch


@pytest.mark.parametrize("mala,entran", [("BTC", ["ETH", "SOL"]), ("ETH", ["BTC", "SOL"])])
async def test_motor_ruptura3_una_moneda_sin_datos_no_frena_a_las_demas(monkeypatch, mala, entran):
    """Las velas de 4 h de una moneda fallan: solo ella espera (10 s, luego 20 s…) y las demás siguen en la misma ronda.
    Antes, el fallo cortaba el bucle en esa moneda (las de detrás no se miraban) y, si era la primera, la espera de todas
    subía hasta 5 min y sus rupturas llegaban «tarde»."""
    monkeypatch.setattr(config, "USER_RULES", True)
    now = [T0 + 20 * MIN + 3000]                                          # la vela de 5 min de las 9:15 acaba de cerrar
    dec = FakeDec()
    eng, sent, data = rp_engine(now, {c: sube_rompe() for c in ("BTC", "ETH", "SOL")}, dec=dec)
    _falla(eng, data, lambda c, i: c == mala and i == "4h")
    await eng.step()
    w = _w(eng)
    assert [a[0]["coin"] for a in sent] == entran                         # las otras dos entran con su ruptura
    assert w["runs"][mala].ctx is None and not w["bars"].get(mala) and not w["runs"][mala].done
    x = w["espera"][mala]
    assert (x["n"], x["hasta"]) == (1, now[0] + 10_000) and x["error"].startswith("OSError") and set(w["espera"]) == {mala}
    n = len(data.fallos)
    now[0] += 5000                                                        # dentro de su espera: ni lo intenta
    await eng.step()
    assert len(data.fallos) == n
    now[0] += 6000                                                        # pasada: lo intenta y espera el doble
    await eng.step()
    assert len(data.fallos) == n + 1 and (x["n"], x["hasta"]) == (2, now[0] + 20_000)


async def test_motor_ruptura3_el_regimen_va_antes_y_con_su_propia_espera(monkeypatch):
    """Sin el régimen (los cierres diarios de una moneda caídos) ninguna moneda pide su contexto ni sus velas: el régimen
    espera solo (10 s, 20 s…), guarda lo que ya bajó y, cuando vuelve, todas siguen."""
    monkeypatch.setattr(config, "USER_RULES", True)
    now = [T0 + 2 * MIN]
    eng, sent, data = rp_engine(now, {"BTC": sube_rompe(), "ETH": sube_rompe()}, dec=FakeDec())
    caido = [True]
    _falla(eng, data, lambda c, i: caido[0] and c == "SOL" and i == "1d")
    await eng.step()
    w = _w(eng)
    assert "rp_regimen" not in w["ctx"] and set(w["ctx"]["rp_d1"]) == {"BTC", "ETH"}   # lo bajado se guarda
    assert (w["espera"][ap.REGIMEN]["n"], w["espera"][ap.REGIMEN]["hasta"]) == (1, now[0] + 10_000)
    assert not [x for x in data.calls if x[1] != "1d"] and all(r.ctx is None for r in w["runs"].values())
    now[0] += 5000                                                        # esperando: ni lo intenta
    await eng.step()
    assert data.fallos == [("SOL", "1d")]
    caido[0] = False
    now[0] += 6000
    await eng.step()
    assert w["ctx"]["rp_regimen"]["zona"] == "RISK_ON" and ap.REGIMEN not in w["espera"]
    assert [c for c, i in data.calls if i == "1d"].count("ETH") == 1      # ETH no se volvió a pedir
    assert all(r.ctx is not None and r.ctx["rp"]["zona"] == "RISK_ON" for r in w["runs"].values()) and not sent


async def test_motor_ruptura3_sin_regimen_en_toda_la_sesion_acaba_sin_contexto():
    """Si el régimen no llega en toda la sesión, cada moneda acaba con «sin_contexto» y el porqué (no «sin_velas» ni
    «no llegaron las velas de 1 min», que no es la causa), con el último error y los intentos."""
    now = [T0 + 2 * MIN]
    dec = FakeDec()
    eng, sent, data = rp_engine(now, {"BTC": sube_rompe(), "ETH": sube_rompe()}, dec=dec)
    _falla(eng, data, lambda c, i: i == "1d")
    await eng.step()
    now[0] = T0 + 242 * MIN + 1000                                        # fin de la sesión (+240 min y 2 de margen)
    await eng.step()
    fin = {r["coin"]: r for r in dec.rows if r["decision"] == "no_entra"}
    assert set(fin) == {"BTC", "ETH"} and not sent
    for c, r in fin.items():
        assert (r["fase"], r["motivo_codigo"]) == ("medida", "sin_contexto") and _w(eng)["runs"][c].done
        assert r["motivo"] == ("Fin de la sesión sin contexto: no llegó el régimen cripto (los cierres diarios de "
                               "Binance); no se pudo decidir")
        assert r["datos"] == {"error": "OSError: 1d de BTC caídas", "intentos": 1}


async def test_motor_impulso_sin_contexto_en_toda_la_sesion_no_es_sin_velas():
    """En el modelo de siempre (aquí «6 diarias v2») pasa lo mismo: si las velas de 1 h de la moneda (su contexto) no
    llegan en toda la sesión, su fila es «sin_contexto», no «no llegaron las velas de 1 min»."""
    now = [T0 + 2 * MIN]
    dec = FakeDec()
    eng = ap.OpeningEngine(FakeBus(), None, lambda c, w: {"dir": 0, "strength": 0.0, "why": ""}, lambda *a: True,
                           lambda: {"positions": [], "pending": [], "closed": []}, clock=lambda: now[0],
                           params={**ap.SEIS2, "windows": ["europa"], "coins": ["ETH"]}, decisiones=dec)
    data = RpData(now, {"ETH": sube_rompe()})
    eng.fetch_interval = data.fetch_interval
    _falla(eng, data, lambda c, i: i == "1h")
    await eng.step()
    assert _w(eng)["espera"]["ETH"]["n"] == 1
    now[0] = T0 + 122 * MIN + 1000                                        # fin de la sesión de «6 diarias v2» (+120 min)
    await eng.step()
    r = dec.rows[-1]
    assert (r["fase"], r["decision"], r["motivo_codigo"]) == ("medida", "no_entra", "sin_contexto")
    assert r["motivo"] == ("Fin de la sesión sin contexto: no llegaron de Binance los datos de ETH para su contexto "
                           "(velas de 1 h o de la apertura anterior); no se pudo decidir")
    assert r["datos"]["error"] == "OSError: 1h de ETH caídas"


async def test_motor_ruptura3_funding_viejo_no_vale_para_el_regimen(monkeypatch, caplog):
    """Una última liquidación de funding de hace más de 16 h (de Binance o del archivo que pone al día la vigilancia) no
    se usa: si no hay otra, el componente de funding de esa moneda queda sin dato (su peso se reparte) y queda en
    bot.log. Una de hace 8 h, sí."""
    from bot import historia_extra
    now = [T0 + 16 * MIN]
    eng, _, _ = rp_engine(now, {"BTC": sube_rompe()})
    viejo = [[T0 - 72 * ap.H, 0.0002], [T0 - 64 * ap.H, 0.0003]]          # la vigilancia lleva días sin bajarlo
    fresco = [[T0 - 16 * ap.H, 0.0001], [T0 - 8 * ap.H, 0.0004]]

    async def binance_viejo(coin, end_ms):
        return viejo

    eng.fetch_funding = binance_viejo
    monkeypatch.setattr(historia_extra, "load_funding", lambda sym, base=None: fresco if sym == "ETH" else viejo)
    with caplog.at_level(logging.WARNING, logger="apertura"):
        await eng.step()
    f8 = _w(eng)["ctx"]["rp_regimen"]["funding_8h"]
    assert f8["ETH"] == pytest.approx(0.0004)                             # Binance viejo: vale el guardado, de hace 8 h
    assert f8["BTC"] is None and f8["SOL"] is None and f8["XRP"] is None  # los dos viejos: sin dato
    assert "es de hace 64 h" in caplog.text and "queda sin dato" in caplog.text


async def test_motor_ruptura3_sin_xperps_veta_los_cortos_y_las_compras_entran(monkeypatch):
    """Con el perfil de comisiones sin cuenta X-Perps (contado 0,35 %), el corto de un RISK_OFF se veta («sin_cortos»):
    el resultado de fuera de muestra, que sale de los cortos, no existiría. La compra de un RISK_ON sí entra: el suelo de
    2R (+4 %) deja 3,26 % neto con 0,74 % de ida y vuelta, ≥ 3 %."""
    from bot import comisiones
    monkeypatch.setattr(config, "USER_RULES", True)
    assert comisiones.elegir(comisiones.SIN_XPERPS)[0] and not comisiones.hay_cortos()
    now = [T0 + 20 * MIN + 3000]
    baja = base_bars() + tramo(15, [99.8, 99.6, 99.4, 99.2, 99.0], v=30.0) + flat_until(20, 30, 99.0)
    dec = FakeDec()
    eng, sent, _ = rp_engine(now, {"ETH": baja}, sube=False, dec=dec)
    await eng.step()
    run = _w(eng)["runs"]["ETH"]
    assert not sent and run.ctx["rp"]["zona"] == "RISK_OFF" and run.dir == -1 and run.done
    r = dec.rows[-1]
    assert (r["fase"], r["decision"], r["motivo_codigo"]) == ("entrada", "no_entra", "sin_cortos")
    assert "sin cuenta X-Perps no hay cortos" in r["motivo"]
    dec = FakeDec()
    eng, sent, _ = rp_engine(now, {"BTC": sube_rompe()}, dec=dec)
    await eng.step()
    [(t, story, hl, strat, win)] = sent
    assert t["instrument"] == "spot" and strat == "impulso-ruptura3-largo-sin-noticia"
    plan = dec.rows[-1]["datos"]["plan"]
    assert dec.rows[-1]["motivo_codigo"] == "entra" and plan["coste_pct"] == pytest.approx(0.74)
