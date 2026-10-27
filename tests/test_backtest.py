import asyncio
import json
import os
import random
import shutil
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from bot import backtest as bt
from bot import config, history, regions

H = 3600_000
TEST_TMP = Path(os.environ.get("BOT_TEST_TMP", r"M:\BotFundamental-runtime\tmp-pruebas"))
CONTRACT_KEYS = {"id", "family", "name", "description", "n", "hit_rate", "base_rate", "lift_pp", "p_value", "avg_ret_pct",
                 "total_ret_pct", "win_rate", "profit_factor", "max_dd_pct", "sharpe", "trades_per_year", "actionable",
                 "oos", "verdict", "verdict_why", "equity", "params"}


@pytest.fixture
def mtmp():
    """Carpeta temporal en M: (los datos nunca van a C:); si no se puede crear, la prueba se salta."""
    try:
        TEST_TMP.mkdir(parents=True, exist_ok=True)
        d = Path(tempfile.mkdtemp(dir=TEST_TMP))
    except OSError as e:
        pytest.skip(f"No se puede crear la carpeta temporal en M: ({e})")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def utc(*a) -> int:
    return int(datetime(*a, tzinfo=timezone.utc).timestamp() * 1000)


def weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def pair_rows(n=700, follow=0.8, seed=7):
    """A = índice que manda; B copia el signo de A (de apertura a cierre) con probabilidad `follow`; None = ruido."""
    rng = random.Random(seed)
    a_rows, b_rows, a_prev, b_prev = [], [], 100.0, 100.0
    for d in weekdays(date(2022, 1, 3), n):
        ar = rng.choice((1, -1)) * rng.uniform(0.2, 1.5)
        a_o, a_c = a_prev * (1 + rng.uniform(-0.2, 0.2) / 100), a_prev * (1 + ar / 100)
        a_rows.append([d.isoformat(), a_o, max(a_o, a_c) * 1.003, min(a_o, a_c) * 0.997, a_c])
        same = 1 if ar > 0 else -1
        s = rng.choice((1, -1)) if follow is None else (same if rng.random() < follow else -same)
        b_o = b_prev * (1 + rng.choice((1, -1)) * rng.uniform(0.05, 0.3) / 100)
        b_c = b_o * (1 + s * rng.uniform(0.3, 1.5) / 100)
        b_rows.append([d.isoformat(), b_o, max(b_o, b_c) * 1.003, min(b_o, b_c) * 0.997, b_c])
        a_prev, b_prev = a_c, b_c
    return a_rows, b_rows


def candle_rows(ohlc, t0=utc(2026, 1, 5)):
    return [[t0 + k * H, o, h, l, c, 0.0] for k, (o, h, l, c) in enumerate(ohlc)]


def trend_rows(n, t0, step=0.005):
    out, p = [], 100.0
    for k in range(n):
        c = p * (1 + step)
        out.append([t0 + k * H, p, max(p, c) * 1.001, min(p, c) * 0.999, c, 0.0])
        p = c
    return out


# ---------- Estadística ----------
def test_p_valor_binomial_unilateral():
    assert bt.binom_sf(8, 10, 0.5) == pytest.approx(56 / 1024)
    assert bt.binom_sf(60, 100, 0.5) == pytest.approx(0.0284440, abs=1e-6)
    assert bt.binom_sf(0, 10, 0.3) == 1.0 and bt.binom_sf(11, 10, 0.3) == 0.0
    assert bt.binom_sf(1400, 2500, 0.5) < 1e-8 < bt.binom_sf(1250, 2500, 0.5)


def test_comisiones_por_lado(monkeypatch):
    monkeypatch.setattr(config, "FEE_PCT", 0.10)
    m = bt.evaluate([(1, 1.0, 0.5), (2, -0.5, 0.5), (3, 0.15, 0.5)])
    assert m["hit_rate"] == pytest.approx(2 / 3, abs=1e-4)       # acierta la dirección dos veces…
    assert m["win_rate"] == pytest.approx(1 / 3, abs=1e-4)       # …pero +0,15 % no cubre el 0,20 % de ida y vuelta
    assert m["avg_ret_pct"] == pytest.approx((0.8 - 0.7 - 0.05) / 3, abs=1e-4)
    assert m["profit_factor"] == pytest.approx(0.8 / 0.75, abs=1e-3)
    assert m["total_ret_pct"] == pytest.approx((1.008 * 0.993 * 0.9995 - 1) * 100, abs=1e-2)
    assert m["max_dd_pct"] == pytest.approx((1.008 - 1.008 * 0.993 * 0.9995) / 1.008 * 100, abs=1e-2)
    monkeypatch.setattr(config, "FEE_PCT", 0.0)
    assert bt.evaluate([(1, 1.0, 0.5), (2, -0.5, 0.5)])["avg_ret_pct"] == pytest.approx(0.25)


def test_reglas_de_veredicto():
    ok = {"n": 250, "hit_rate": 0.6, "base_rate": 0.5, "lift_pp": 10.0, "p_value": 0.001, "avg_ret_pct": 0.1,
          "oos": {"hit_rate": 0.58, "avg_ret_pct": 0.05}}
    assert bt.verdict(ok)[0] == "funciona"
    assert bt.verdict({**ok, "n": 99})[0] == "pocos_datos"
    assert bt.verdict({**ok, "n": 150})[0] == "dudosa"
    assert bt.verdict({**ok, "oos": {"hit_rate": 0.49, "avg_ret_pct": 0.05}})[0] == "dudosa"
    v, why = bt.verdict({**ok, "avg_ret_pct": -0.02})
    assert v == "dudosa" and "comisiones" in why
    assert bt.verdict({**ok, "p_value": 0.2})[0] == "no_funciona"
    assert bt.verdict({**ok, "lift_pp": 1.0})[0] == "no_funciona"
    # fuera de muestra se compara también con la base de ese tramo (deriva alcista)
    v, why = bt.verdict({**ok, "oos": {"hit_rate": 0.58, "avg_ret_pct": 0.05, "base_rate": 0.62}})
    assert v == "dudosa" and "62,0 %" in why
    # ganancia neta no significativa y corrección por comparaciones múltiples
    v, why = bt.verdict({**ok, "params": {"p_net": 0.3}})
    assert v == "dudosa" and "ganancia neta no es significativa" in why
    v, why = bt.verdict({**ok, "params": {"p_net": 0.001, "q_value": 0.2, "n_pruebas": 580}})
    assert v == "dudosa" and "comparaciones múltiples" in why and "580 pruebas" in why
    assert bt.verdict({**ok, "params": {"p_net": 0.001, "q_value": 0.01, "n_pruebas": 580}})[0] == "funciona"


def test_lo_no_operable_nunca_funciona():
    ok = {"n": 2400, "hit_rate": 0.79, "base_rate": 0.53, "lift_pp": 26.0, "p_value": 1e-90, "avg_ret_pct": 0.5,
          "oos": {"hit_rate": 0.78, "avg_ret_pct": 0.4, "base_rate": 0.53}, "actionable": True,
          "params": {"tipo": "relacion", "a": "frankfurt", "b": "madrid", "cierre": "a_la_vez", "q_value": 1e-80}}
    v, why = bt.verdict(ok)
    assert v == "solo_correlacion"
    assert why.startswith("Fráncfort y Madrid se mueven juntas, pero ambas cierran casi a la vez: no da tiempo a operar.")
    late = bt.verdict({**ok, "params": {**ok["params"], "cierre": "despues", "a": "nuevayork", "b": "tokio"}})[1]
    assert late.startswith("Nueva York y Tokio se mueven juntas, pero Nueva York aún no ha cerrado cuando abre Tokio")
    # cualquier estrategia sin la información a tiempo, aunque no sea una relación
    v, why = bt.verdict({**ok, "actionable": False, "params": {"tipo": "operable", "p_net": 0.0}})
    assert v == "solo_correlacion" and "no llega a tiempo" in why
    # si la relación es floja sigue siendo no_funciona o dudosa, con la nota de que no se opera
    v, why = bt.verdict({**ok, "p_value": 0.3})
    assert v == "no_funciona" and "Solo informativa" in why
    assert bt.verdict({**ok, "n": 150})[0] == "dudosa"
    assert "solo_correlacion" in bt.VERDICT_ORDER


def test_relacion_entre_bolsas_que_cierran_a_la_vez():
    a, b = pair_rows(follow=0.8)
    idx = {"frankfurt": bt.prepare_index(a), "madrid": bt.prepare_index(b)}
    st = {s["id"]: s for s in bt.chain_strategies(idx, date(2026, 10, 23))}
    rel = st["encadenamiento-frankfurt-madrid-sube"]
    assert not rel["actionable"] and rel["params"]["cierre"] == "a_la_vez" and rel["hit_rate"] > 0.7
    assert rel["verdict"] == "solo_correlacion"
    assert rel["verdict_why"].startswith("Fráncfort y Madrid se mueven juntas, pero ambas cierran casi a la vez")
    assert not any(s["verdict"] == "funciona" and not bt._tradable(s) for s in st.values())


def test_benjamini_hochberg():
    q = bt.bh_qvalues([0.01, 0.04, 0.03, 0.5])
    assert q == pytest.approx([0.04, 0.04 * 4 / 3, 0.04 * 4 / 3, 0.5])
    assert bt.bh_qvalues([]) == [] and bt.bh_qvalues([0.2]) == [0.2]


def fake_strategy(sid, p, family="flujo", n=300, p_net=0.001, **kw):
    s = {"id": sid, "family": family, "name": sid, "description": "", "n": n, "hit_rate": 0.56, "base_rate": 0.5,
         "lift_pp": 6.0, "p_value": p, "avg_ret_pct": 0.1, "total_ret_pct": 10.0, "win_rate": 0.55, "profit_factor": 1.2,
         "max_dd_pct": 5.0, "sharpe": 0.5, "trades_per_year": 50.0, "actionable": True,
         "oos": {"n": 90, "hit_rate": 0.55, "avg_ret_pct": 0.05, "base_rate": 0.5}, "verdict": "", "verdict_why": "",
         "equity": [], "params": {"p_net": p_net}}
    s.update(kw)
    return bt.judge(s)


def test_correccion_por_comparaciones_multiples():
    lone = [fake_strategy("unica", 0.04)]
    bt.finalize(lone)
    assert lone[0]["params"]["q_value"] == 0.04 and lone[0]["verdict"] == "funciona"
    many = [fake_strategy("borde", 0.04)] + [fake_strategy(f"ruido{k}", 0.5) for k in range(20)]
    assert many[0]["verdict"] == "funciona"                      # suelta pasaría…
    bt.finalize(many)
    assert many[0]["verdict"] == "dudosa" and "comparaciones múltiples" in many[0]["verdict_why"]   # …con 21 pruebas no
    assert many[0]["params"]["n_pruebas"] == 21 and many[0]["params"]["q_value"] == 0.5
    # la ganancia neta también cuenta: con p_net malo la q sale de él aunque el acierto sea muy significativo
    s = [fake_strategy("neta", 1e-6, p_net=0.2)]
    bt.finalize(s)
    assert s[0]["params"]["q_value"] == 0.2 and s[0]["verdict"] == "dudosa"
    # las relaciones van en su propio grupo y no abaratan la corrección de las operables
    rel = fake_strategy("rel", 1e-30, params={"tipo": "relacion", "a": "tokio", "b": "madrid", "cierre": "antes",
                                              "direccion": "sube"})
    mix = [fake_strategy("borde", 0.04), fake_strategy("otra", 0.5), rel]
    bt.finalize(mix)
    assert mix[0]["params"]["n_pruebas"] == 2 and mix[2]["params"]["n_pruebas"] == 1
    assert mix[2]["verdict"] == "solo_correlacion"


def test_p_valor_de_la_ganancia_neta(monkeypatch):
    monkeypatch.setattr(config, "FEE_PCT", 0.10)
    rng = random.Random(1)
    # acierta la dirección el 60 % pero gana muy poco: +0,25 / −0,2 brutos, y la comisión se come la ventaja
    tr = [(k, 0.25 if rng.random() < 0.6 else -0.2, 0.5) for k in range(400)]
    m = bt.evaluate(tr)
    assert m["hit_rate"] > 0.55 and m["avg_ret_pct"] < 0 and m["p_net"] > 0.5
    good = bt.evaluate([(k, 1.0 if k % 3 else -0.6, 0.5) for k in range(400)])
    assert good["avg_ret_pct"] > 0 and good["p_net"] < 0.001


def test_base_fuera_de_muestra_del_propio_tramo():
    # el universo sube el 50 % de las veces en el 70 % inicial y el 90 % en el tramo final (deriva alcista)
    pool = [(k, None, k % 2 == 0) for k in range(700)] + [(k, None, k % 10 != 0) for k in range(700, 1000)]
    trades = [(k, 1.0 if ok else -1.0, 0.62) for k, _, ok in pool if k % 5 == 0]
    m = bt.evaluate(trades, base_after=bt._pool_base(pool))
    assert m["oos"]["base_rate"] == pytest.approx(0.9, abs=0.01) and m["base_rate"] == pytest.approx(0.62)
    assert bt.evaluate(trades)["oos"]["base_rate"] == pytest.approx(0.62)   # sin universo, la de toda la muestra


def test_apertura_de_yahoo_marcada():
    a, b = pair_rows(follow=0.8)
    st = {s["id"]: s for s in bt.chain_strategies({"tokio": bt.prepare_index(a), "madrid": bt.prepare_index(b)},
                                                  date(2026, 10, 23))}
    s = st["encadenamiento-tokio-madrid-compra"]
    assert s["params"]["apertura"] == bt.YAHOO_OPEN and "Yahoo" in s["description"] and "ETF" in s["verdict_why"]


def test_caches_de_fechas_se_vacian_tras_compute(mtmp, monkeypatch):
    monkeypatch.setattr(config, "HIST_DIR", mtmp / "hist")
    monkeypatch.setattr(config, "BACKTEST_DIR", mtmp / "bt")
    a, b = pair_rows(n=150)
    for key, rows in (("tokio", a), ("madrid", b)):
        history._write(history.index_path(key), {"fetched": 0, "rows": rows})
    bt.compute(store=[])
    assert bt._open_ms.cache_info().currsize == bt._close_ms.cache_info().currsize == 0
    assert bt.prev_weekday.cache_info().currsize == 0


def test_curva_de_capital_como_mucho_200_puntos():
    m = bt.evaluate([(k, 0.5, 0.5) for k in range(1000)])
    assert len(m["equity"]) == 200 and m["equity"][0] == [0, 100.0] and m["equity"][-1][0] == 999


# ---------- Encadenamiento entre bolsas ----------
def test_b_copia_el_signo_de_a_el_80_por_ciento():
    a, b = pair_rows(follow=0.8)
    idx = {"tokio": bt.prepare_index(a), "madrid": bt.prepare_index(b)}
    st = {s["id"]: s for s in bt.chain_strategies(idx, date(2026, 10, 23))}
    s = st["encadenamiento-tokio-madrid-compra"]
    assert CONTRACT_KEYS <= set(s)
    assert abs(s["hit_rate"] - 0.8) < 0.06 and abs(s["base_rate"] - 0.5) < 0.06 and s["lift_pp"] > 20
    assert s["n"] >= 200 and s["p_value"] < 0.001 and s["actionable"] and s["verdict"] == "funciona"
    assert s["oos"]["n"] == s["n"] - round(s["n"] * 0.7) and s["oos"]["hit_rate"] > s["base_rate"]
    short = st["encadenamiento-tokio-madrid-venta"]           # cortos: si Tokio cae, vender Madrid en la apertura
    assert short["side"] == "short" and abs(short["hit_rate"] - 0.8) < 0.06
    assert short["avg_ret_pct"] > 0 and short["verdict"] == "funciona"
    rel = st["encadenamiento-tokio-madrid-sube"]               # relación de cierre a cierre (la de la web)
    assert rel["params"]["tipo"] == "relacion" and rel["actionable"] and rel["hit_rate"] > 0.7
    assert rel["params"]["operable_id"] == "encadenamiento-tokio-madrid-compra" and "Versión operable" in rel["verdict_why"]
    assert rel["verdict"] == "solo_correlacion" and rel["params"]["cierre"] == "antes"   # firme, pero no se opera tal cual
    assert rel["verdict_why"].startswith("Tokio y Madrid se mueven juntas, pero de cierre a cierre")
    # Madrid cierra después de que abra Tokio: solo vale la sesión anterior (Madrid ayer → Tokio hoy)
    assert "encadenamiento-madrid_ayer-tokio-compra" in st and "encadenamiento-madrid-tokio-compra" not in st
    assert not st["encadenamiento-madrid-tokio-sube"]["actionable"]


def test_ruido_no_funciona():
    a, b = pair_rows(follow=None, seed=11)
    idx = {"tokio": bt.prepare_index(a), "madrid": bt.prepare_index(b)}
    st = {s["id"]: s for s in bt.chain_strategies(idx, date(2026, 10, 23))}
    for sid in ("encadenamiento-tokio-madrid-compra", "encadenamiento-tokio-madrid-venta"):
        assert st[sid]["verdict"] == "no_funciona" and abs(st[sid]["lift_pp"]) < 8 and st[sid]["n"] > 300


def test_alineacion_de_fechas_con_festivos():
    days = weekdays(date(2024, 1, 1), 10)                       # lunes 1 de enero de 2024 en adelante
    holiday = date(2024, 1, 3)
    a = [[d.isoformat(), 100 + k, 101 + k, 99 + k, 100.5 + k] for k, d in enumerate(days) if d != holiday]
    b = [[d.isoformat(), 200 + k, 202 + k, 198 + k, 201 + k] for k, d in enumerate(days)]
    A, B = bt.prepare_index(a), bt.prepare_index(b)
    same = [d for d, _, _ in bt.align(A, B, 0)]
    assert holiday not in same and date(2024, 1, 4) in same and date(2024, 1, 1) not in same
    # el jueves de A se mide contra el martes, la última sesión que hubo
    assert A[date(2024, 1, 4)].cc == pytest.approx((103.5 / 101.5 - 1) * 100)
    prev = {d: x for d, x, _ in bt.align(A, B, 1)}
    assert date(2024, 1, 4) not in prev                          # la víspera fue festivo en A: se salta
    assert prev[date(2024, 1, 8)] is A[date(2024, 1, 5)]         # lunes ← viernes


def test_apertura_que_repite_el_cierre_anterior_no_sirve_para_operar():
    a, b = pair_rows(n=300)
    stale = [r[:1] + [b[k - 1][4] if k else r[1]] + r[2:] for k, r in enumerate(b)]   # como FTSE 100 en Yahoo
    assert bt.stale_open_share(stale) > 0.9 and bt.stale_open_share(b) == 0
    idx = {"tokio": bt.prepare_index(a), "madrid": bt.prepare_index(stale)}
    assert all(d.oc is None for d in idx["madrid"].values())
    st = {s["id"] for s in bt.chain_strategies(idx, date(2026, 10, 23))}
    assert "encadenamiento-tokio-madrid-sube" in st and "encadenamiento-tokio-madrid-compra" not in st


def test_combinaciones_de_dos_bolsas_y_matriz():
    a, b = pair_rows(follow=0.8)
    c, _ = pair_rows(follow=0.8, seed=3)
    idx = {"tokio": bt.prepare_index(a), "shanghai": bt.prepare_index(c), "frankfurt": bt.prepare_index(b)}
    st = {s["id"]: s for s in bt.chain_strategies(idx, date(2026, 1, 15))}
    combo = st["encadenamiento-tokio+shanghai-frankfurt-compra"]
    assert combo["name"].startswith("Si Tokio y Shanghái cierran en positivo") and combo["n"] > 0
    # Shanghái cierra a las 07:00 UTC: en verano coincide con la apertura de Fráncfort y, con el margen de 15 min,
    # solo cuentan los días de invierno
    assert 0.25 < combo["params"]["share_operable"] < 0.6
    ll = bt.leadlag_matrix(idx, date(2026, 1, 15))
    k = ll["markets"]
    assert k == ["tokio", "shanghai", "frankfurt"] and len(ll["p_up_given_up"]) == 3 and len(ll["lag1"]["n"]) == 3
    assert ll["actionable"][k.index("tokio")][k.index("frankfurt")] and not ll["actionable"][k.index("frankfurt")][k.index("tokio")]
    assert ll["actionable"][k.index("shanghai")][k.index("frankfurt")]                   # en invierno sí
    assert not bt.leadlag_matrix(idx, date(2026, 7, 15))["actionable"][k.index("shanghai")][k.index("frankfurt")]
    assert ll["p_up_given_up"][k.index("tokio")][k.index("frankfurt")] > 0.7 and ll["n"][0][0] > 600


# ---------- Cripto ----------
def test_stop_dinamico_largo_corto_y_hueco():
    up = bt.Candles(candle_rows([(100, 102, 99.5, 101.5), (101.5, 106, 101, 105.5), (105.5, 110, 105, 109),
                                 (109, 109.5, 100, 101)]))
    assert up.trail(0, 4, 1, 2.0) == pytest.approx(7.8)         # máximo 110 → stop 107,8
    assert up.trail(0, 3, 1, 2.0) == pytest.approx(9.0)         # sin tocar el stop sale al cierre
    assert up.fixed(0, 4, 1) == pytest.approx(1.0) and up.fixed(0, 4, -1) == pytest.approx(-1.0)
    down = bt.Candles(candle_rows([(100, 100.5, 98, 98.5), (98.5, 99, 94, 94.5), (94.5, 95, 90, 91), (91, 100, 90.5, 99)]))
    assert down.trail(0, 4, -1, 2.0) == pytest.approx(8.2)      # corto: mínimo 90 → stop 91,8
    gap = bt.Candles(candle_rows([(100, 103, 99.5, 102.5), (95, 96, 94, 95.5)]))
    assert gap.trail(0, 2, 1, 2.0) == pytest.approx(-5.0)       # abre por debajo del stop: sale a la apertura
    # automático: 1,5 veces el rango medio de las 24 h previas (1 % → stop al 1,5 %)
    auto = bt.Candles(candle_rows([(100, 100.5, 99.5, 100)] * 24 + [(100, 104, 99.9, 103.5), (103.5, 103.6, 101, 101.5)]))
    assert auto.trail_auto(24, 1, kmax=2) == pytest.approx((104 * 0.985 / 100 - 1) * 100)


def test_ventanas_con_horario_de_verano():
    assert bt.window_start_ms("londres", date(2025, 7, 1)) == utc(2025, 7, 1, 7)
    assert bt.window_start_ms("londres", date(2025, 12, 1)) == utc(2025, 12, 1, 8)
    assert bt.window_start_ms("nuevayork", date(2025, 7, 1)) == utc(2025, 7, 1, 13)   # 09:30 EDT → 13:30 → 13:00
    assert bt.window_start_ms("macro", date(2025, 12, 1)) == utc(2025, 12, 1, 13)     # 08:30 EST = 13:30 UTC
    assert bt.window_start_ms("tokio", date(2025, 7, 1)) == bt.window_start_ms("cierre", date(2025, 7, 1)) == utc(2025, 7, 1)


def test_cierre_con_margen_en_la_apertura_de_londres():
    summer, winter = date(2025, 7, 1), date(2025, 12, 1)
    day = {summer: bt.Day(100, 101, 1.0, 1.0), winter: bt.Day(100, 99, -1.0, -1.0)}
    idx = {"hongkong": day, "shanghai": day, "tokio": day}
    lon = lambda d: bt.window_start_ms("londres", d)
    # Hong Kong fija su cierre en subasta (16:00-16:10 HKT = 08:00-08:10 UTC): ni en invierno se conoce a las 08:00 UTC
    assert bt.asia_signal(idx, summer, lon(summer), "hongkong") is None
    assert bt.asia_signal(idx, winter, lon(winter), "hongkong") is None
    assert bt.asia_signal(idx, summer, bt.window_start_ms("nuevayork", summer), "hongkong") == 1
    # Shanghái cierra a las 07:00 UTC, justo cuando abre Londres en verano: sin margen no se puede usar
    assert bt.asia_signal(idx, summer, lon(summer), "shanghai") is None
    assert bt.asia_signal(idx, winter, lon(winter), "shanghai") == -1
    assert bt.asia_signal(idx, summer, lon(summer), "tokio") == 1               # 06:30 UTC + 15 min ≤ 07:00
    assert not bt.known("hongkong", winter, "frankfurt", winter) and bt.known("tokio", summer, "frankfurt", summer)
    assert not bt.known("shanghai", summer, "londres", summer) and bt.known("shanghai", winter, "londres", winter)


def test_bolsa_a_cripto_en_la_apertura_de_londres():
    rng = random.Random(5)
    days = weekdays(date(2025, 1, 6), 260)
    tokio_rows, prev, signs = [], 100.0, {}
    for d in days:
        c = prev * (1 + rng.choice((1, -1)) * rng.uniform(0.2, 1.0) / 100)
        tokio_rows.append([d.isoformat(), prev * 1.001, max(prev, c) * 1.01, min(prev, c) * 0.99, c])
        signs[d] = 1 if c > prev else -1
        prev = c
    starts = {bt.window_start_ms("londres", d): d for d in days}
    t0, rows, p, drift_left, drift = utc(2025, 1, 1), [], 100.0, 0, 0.0
    for k in range(400 * 24):
        t = t0 + k * H
        if t in starts:
            s = signs[starts[t]] if rng.random() < 0.8 else -signs[starts[t]]
            drift_left, drift = 4, s * 0.3
        r = (drift if drift_left else 0) + rng.uniform(-0.05, 0.05)
        drift_left = max(0, drift_left - 1)
        c = p * (1 + r / 100)
        rows.append([t, p, max(p, c) * 1.0005, min(p, c) * 0.9995, c, 0.0])
        p = c
    st = {s["id"]: s for s in bt.stock_to_crypto({"tokio": bt.prepare_index(tokio_rows)}, {"BTC": bt.Candles(rows)})}
    s = st["bolsa_a_cripto-tokio-btc-londres-compra"]
    assert abs(s["hit_rate"] - 0.8) < 0.08 and s["coin"] == "BTC" and s["family"] == "bolsa_a_cripto"
    assert st["bolsa_a_cripto-tokio-btc-londres-venta"]["avg_ret_pct"] > 0
    assert st["bolsa_a_cripto-tokio-btc-londres-compra-trailing"]["params"]["stop"] == "trailing"


def test_ventanas_cripto_generan_las_tres_reglas_con_y_sin_stop():
    rng = random.Random(9)
    rows, p = [], 100.0
    for k in range(60 * 24):
        c = p * (1 + rng.gauss(0, 0.4) / 100)
        rows.append([utc(2025, 3, 1) + k * H, p, max(p, c) * 1.002, min(p, c) * 0.998, c, 0.0])
        p = c
    sts = bt.crypto_windows({}, {"ETH": bt.Candles(rows)})
    ids = {s["id"] for s in sts}
    assert len(ids) == len(sts) == 5 * 3 * 2
    assert {"ventana_cripto-londres-ruptura-eth", "ventana_cripto-macro-contra-eth-trailing",
            "ventana_cripto-cierre-tendencia-eth"} <= ids
    assert all(CONTRACT_KEYS <= set(s) for s in sts)


def test_noticias_se_llenan_con_el_tiempo():
    t0 = utc(2026, 10, 1)
    cd = bt.Candles(trend_rows(48, t0))
    tri = lambda d, m: {"triage": {"repercusion": {"direccion": d, "magnitud": m, "activos": ["BTC"]}}}
    evs = [{"ts": t0 + 10 * 60_000, "story": "n1", "coins": ["BTC"], "patch": tri(80, 60)},     # alcista fuerte
           {"ts": t0 + 3 * H + 5, "story": "n2", "coins": ["BTC"], "patch": tri(-70, 50)},     # bajista fuerte
           {"ts": t0 + 5 * H, "story": "n3", "coins": ["BTC"], "patch": tri(10, 30)},          # flojo: fuera
           {"ts": t0 + 6 * H, "story": "n4", "coins": ["BTC"],                                 # triaje antiguo
            "patch": {"triage": {"sentimiento": "alcista", "importancia": 7, "monedas": ["BTC"]}}}]
    evs.append({"ts": t0 + 7 * H, "story": "n5", "coins": ["BTC"],                          # malformado: se descarta
                "patch": {"triage": {"repercusion": {"direccion": "mucho", "magnitud": 90}}}})
    s1, s4 = bt.news_backtest(evs, {"BTC": cd})
    assert s1["id"] == "noticias-1h" and s4["id"] == "noticias-4h" and s1["family"] == "noticias"
    assert s1["n"] == 3 and s1["verdict"] == "pocos_datos"
    assert s1["hit_rate"] == pytest.approx(2 / 3, abs=1e-3)      # en una subida continua la bajista falla
    assert s4["n"] == 2                                          # la de las 4:00 se solapa con la de la 1:00
    empty = bt.news_backtest([], {"BTC": cd})
    assert [s["n"] for s in empty] == [0, 0] and all(s["verdict"] == "pocos_datos" for s in empty)


def test_flujo_minorista_sin_operaciones_solapadas():
    t0 = utc(2026, 9, 20)
    ls = [[t0 + k * H, 1.0 + k * 0.01] for k in range(200)]      # siempre en máximos: percentil 100
    cd = bt.Candles(trend_rows(210, t0))
    st = {s["id"]: s for s in bt.flow_strategies({"BTC": ls}, {"BTC": cd})}
    follow, contra = st["flujo-btc-seguir"], st["flujo-btc-contraria"]
    assert follow["n"] == contra["n"] == 32                      # señales de la hora 72 a la 199, una cada 4 h
    assert follow["hit_rate"] == 1.0 and contra["hit_rate"] == 0.0


# ---------- Históricos ----------
YAHOO_N225 = {"chart": {"result": [{
    "meta": {"exchangeTimezoneName": "Asia/Tokyo", "regularMarketPrice": 62693.16, "regularMarketTime": 1792737903,
             "regularMarketDayHigh": 62786.89, "regularMarketDayLow": 61462.61},
    "timestamp": [1792368000, 1792454400, 1792540800, 1792627200, 1792713600],
    "indicators": {"quote": [{"close": [64916.69921875, 64075.421875, 64621.23828125, 62907.83984375, None],
                              "open": [64819.55859375, 64755.48828125, 64885.94921875, 64539.359375, 62667.05078125],
                              "high": [65067.80078125, 64822.21875, 65404.62890625, 64866.44140625, 62786.890625],
                              "low": [63831.78125, 63807.1484375, 64558.6015625, 62907.83984375, 61462.609375]}]}}]}}
KLINES = [[1502928000000, "4261.48000000", "4485.39000000", "4200.74000000", "4285.08000000", "795.15037700",
           1503014399999, "3454770.05073206", 3427, "616.24854100", "2678216.40060401", "0"],
          [1503014400000, "4285.08000000", "4371.52000000", "3938.77000000", "4108.37000000", "1199.88826400",
           1503100799999, "5086958.30617151", 5233, "972.86871000", "4129123.31651808", "0"]]


def test_yahoo_fechas_en_la_zona_de_la_bolsa():
    rows = history.parse_yahoo(YAHOO_N225, "Asia/Tokyo")
    assert [r[0] for r in rows] == ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"]
    assert rows[-1][4] == 62693.16 and rows[0][1] == pytest.approx(64819.5586)   # cierre de hoy desde la cabecera
    midnight = {"chart": {"result": [{"meta": {}, "timestamp": [1729177200],               # 00:00 JST = 15:00 UTC del día 17
                                      "indicators": {"quote": [{"open": [1], "high": [2], "low": [0.5], "close": [1.5]}]}}]}}
    assert history.parse_yahoo(midnight, "Asia/Tokyo")[0][0] == "2024-10-18"
    tokio = regions.MARKETS["tokio"]
    assert len(history.drop_unfinished(rows, tokio, datetime(2026, 10, 23, 5, tzinfo=timezone.utc))) == 4   # abierta
    assert len(history.drop_unfinished(rows, tokio, datetime(2026, 10, 23, 7, tzinfo=timezone.utc))) == 5   # cerrada


def test_binance_descarta_la_vela_en_curso():
    rows = history.parse_klines(KLINES, now_ms=1503050000000)
    assert rows == [[1502928000000, 4261.48, 4485.39, 4200.74, 4285.08, 795.150377]]


class FakeResp:
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


class FakeSession:
    """Simula Yahoo, las velas de Binance (paginadas) y el ratio largo/corto."""

    def __init__(self):
        self.calls = []
        self.now = history._now_ms()

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        p = params or {}
        if "yahoo" in url:
            days = weekdays(date(2025, 1, 6), 150)
            ts = [int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()) for d in days]
            q = {"open": [100 + k for k in range(150)], "high": [102 + k for k in range(150)],
                 "low": [99 + k for k in range(150)], "close": [101 + k for k in range(150)]}
            return FakeResp({"chart": {"result": [{"meta": {}, "timestamp": ts, "indicators": {"quote": [q]}}]}})
        if "klines" in url:
            step = history.STEP_MS[p["interval"]]
            t = max(p["startTime"], utc(2017, 8, 17)) // step * step
            out = []
            while len(out) < p["limit"] and t + step <= self.now + step:
                out.append([t, "1", "2", "0.5", "1.5", "10", t + step - 1, "0", 1, "0", "0", "0"])
                t += step
            return FakeResp(out)
        end = p.get("endTime", self.now) // H * H
        n = 500 if "endTime" not in p else 219
        return FakeResp([{"timestamp": end - k * H, "longShortRatio": "1.5"} for k in range(n)][::-1])


async def test_ensure_history_pagina_cachea_y_no_repite(mtmp, monkeypatch):
    monkeypatch.setattr(config, "HIST_DIR", mtmp)
    monkeypatch.setattr(history, "PAGE_PAUSE_S", 0)
    s = FakeSession()
    info = await history.ensure_history(s)
    assert info["errors"] == []
    assert len(info["indices"]) == sum(1 for m in regions.MARKETS.values() if m.index) == 9
    assert info["coins"]["BTC"][0] == "2017-08-17" and info["coins_1h"]["BTC"][2] >= 729 * 24
    assert info["flow"]["BTC"][2] == 719
    first = len(s.calls)
    await history.ensure_history(s)                              # caché fresca: ni una petición
    assert len(s.calls) == first
    old = history._read(history.klines_path("BTC", "1d"))
    old["fetched"] -= 21 * 3600_000                              # caducada y sin los 3 últimos días: pide solo eso
    old["rows"] = old["rows"][:-3]
    history._write(history.klines_path("BTC", "1d"), old)
    await history.ensure_history(s)
    new = [c for c in s.calls[first:] if "klines" in c[0]]
    assert len(new) == 1 and new[0][1]["startTime"] == old["rows"][-1][0] + 86400_000


def test_compute_y_resultados_json(mtmp, monkeypatch):
    monkeypatch.setattr(config, "HIST_DIR", mtmp / "hist")
    monkeypatch.setattr(config, "BACKTEST_DIR", mtmp / "bt")
    a, b = pair_rows(n=300)
    for key, rows in (("tokio", a), ("madrid", b)):
        history._write(history.index_path(key), {"fetched": 0, "rows": [[r[0]] + [round(x, 4) for x in r[1:]] for r in rows]})
    history._write(history.klines_path("BTC", "1h"), {"fetched": 0, "rows": trend_rows(24 * 30, utc(2023, 2, 1), 0.001)})
    res = bt.compute(store=[])
    assert set(res) >= {"generated", "data", "strategies", "leadlag"} and res["data"]["indices"]["^N225"][2] == 300
    fams = {s["family"] for s in res["strategies"]}
    assert {"encadenamiento", "ventana_cripto", "noticias"} <= fams
    assert all(CONTRACT_KEYS <= set(s) and s["verdict"] in bt.VERDICT_ORDER for s in res["strategies"])
    assert res["leadlag"]["markets"] == ["tokio", "madrid"]
    assert set(res["counts"]) == set(bt.VERDICT_ORDER) and sum(res["counts"].values()) == len(res["strategies"])
    ops = res["counts_operables"]
    assert ops["solo_correlacion"] == 0 and sum(ops.values()) < len(res["strategies"])
    assert all("q_value" in s["params"] for s in res["strategies"] if s["n"] >= 100)
    assert not any(s["verdict"] == "funciona" and not bt._tradable(s) for s in res["strategies"])
    bt.save(res)
    back = bt.load_results()
    assert back["generated"] == res["generated"] and len(back["strategies"]) == len(res["strategies"])
    msg = bt.summary_message(back)
    assert msg["type"] == "backtest" and msg["n"] == len(res["strategies"])
    assert msg["counts_operables"] == res["counts_operables"] and all("avg_ret_pct" in x for x in msg["best"])
    best_ids = {x["id"] for x in msg["best"]}
    assert all(bt._tradable(s) for s in back["strategies"] if s["id"] in best_ids)
    view = bt.filter_results(back, family="encadenamiento", curves=False)
    assert view["strategies"] and all(s["family"] == "encadenamiento" and s["equity"] == [] for s in view["strategies"])
    assert bt.load_results_cached() is bt.load_results_cached()     # no relee si no cambia


# ---------- Noticias desde la base del bot, ejecución y tarea ----------
def triaged(ts, story, d=80, m=60, coins=("BTC",)):
    return {"id": f"e{story}-{ts}", "ts": ts, "story": story, "agent": "local", "kind": "triaged", "coins": list(coins),
            "patch": {"triage": {"repercusion": {"direccion": d, "magnitud": m, "activos": list(coins)}}}}


def no_full_scan(*a):
    raise AssertionError("no debe cargar todos los eventos")


def test_noticias_incrementales_desde_la_base(mtmp, monkeypatch):
    from bot.store import Store
    monkeypatch.setattr(config, "BACKTEST_DIR", mtmp / "bt")
    t0 = utc(2026, 10, 1)
    st = Store(mtmp / "bot.db")
    st.add_event(triaged(t0 + 10 * 60_000, "n1"))
    st.add_event(triaged(t0 + 2 * H, "n2", d=-70, m=50))
    st.add_event({**triaged(t0 + 3 * H, "n3"), "kind": "lowprio"})        # otro tipo: no cuenta
    st.add_event({"id": "raro", "ts": t0 + 4 * H, "story": "n4", "kind": "triaged", "coins": "BTC",
                  "patch": {"triage": {"repercusion": {"direccion": "x", "magnitud": "y", "activos": "BTC"}}}})
    st.events_since = no_full_scan
    cd = bt.Candles(trend_rows(48, t0))
    s1, _ = bt.news_backtest(st, {"BTC": cd})
    assert s1["n"] == 2 and s1["params"]["noticias"] == 2
    cache = json.loads(bt.news_obs_path().read_text(encoding="utf-8"))
    assert cache["last_ts"] == t0 + 4 * H and len(cache["obs"]) == 2
    st.add_event(triaged(t0 + 6 * H, "n6"))
    st.add_event(triaged(t0 + 6 * H + 5, "n1"))                          # misma historia: no se repite
    s1, _ = bt.news_backtest(mtmp / "bot.db", {"BTC": cd})               # también con la ruta
    assert s1["n"] == 3 and len(json.loads(bt.news_obs_path().read_text(encoding="utf-8"))["obs"]) == 3
    st.close()


def write_history(n=300):
    a, b = pair_rows(n=n)
    for key, rows in (("tokio", a), ("madrid", b)):
        history._write(history.index_path(key), {"fetched": 0, "rows": rows})
    history._write(history.klines_path("BTC", "1h"), {"fetched": 0, "rows": trend_rows(24 * 30, utc(2023, 2, 1), 0.001)})


async def test_run_offline_lee_noticias_en_un_hilo(mtmp, monkeypatch):
    from bot.store import Store
    monkeypatch.setattr(config, "HIST_DIR", mtmp / "hist")
    monkeypatch.setattr(config, "BACKTEST_DIR", mtmp / "bt")
    write_history()
    st = Store(mtmp / "bot.db")
    st.add_event(triaged(utc(2023, 2, 5), "n1"))
    st.events_since = no_full_scan
    monkeypatch.setattr(bt, "NEWS_KEEP_MS", 10 * 365 * 86400_000)          # la noticia sintética es de 2023
    res = await bt.run(store=st, offline=True)
    st.close()
    assert res["errors"] == [] and bt.results_path().exists() and bt.news_obs_path().exists()
    assert {s["id"]: s for s in res["strategies"]}["noticias-1h"]["n"] == 1
    assert bt.load_results()["generated"] == res["generated"]


class FakeBus:
    def __init__(self):
        self.store = None
        self.sent = []

    def publish(self, msg):
        self.sent.append(msg)
        return msg


async def test_backtester_sobrevive_a_un_fallo_y_publica(mtmp, monkeypatch):
    monkeypatch.setattr(config, "BACKTEST_DIR", mtmp / "bt")
    monkeypatch.setattr(bt.Backtester, "START_DELAY_S", 0)
    monkeypatch.setattr(bt.Backtester, "CHECK_S", 0.01)
    monkeypatch.setattr(bt, "RETRY_S", 0)
    calls = []

    async def fake_run(session=None, store=None, offline=False):
        calls.append(store)
        if len(calls) == 1:
            raise RuntimeError("compute roto")
        res = {"generated": bt.now_ms(), "strategies": [], "errors": []}
        bt.save(res)
        return res
    monkeypatch.setattr(bt, "run", fake_run)
    bus = FakeBus()
    b = bt.Backtester(bus, None)
    task = asyncio.create_task(b.run())
    for _ in range(300):
        await asyncio.sleep(0.01)
        if bus.sent:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) >= 2 and bus.sent and bus.sent[0]["type"] == "backtest"   # el fallo no paró el bucle
    assert b.next_ms > bt.now_ms() + 19 * 3600_000                                # sin errores: dentro de 20 h
    assert not b.running


async def test_backtester_publica_lo_guardado_y_reintenta_si_hubo_errores(mtmp, monkeypatch):
    monkeypatch.setattr(config, "BACKTEST_DIR", mtmp / "bt")
    bt.save({"generated": bt.now_ms(), "strategies": [], "errors": ["^N225: 429"]})
    bus = FakeBus()
    b = bt.Backtester(bus, None)
    ran = []

    async def fake_run(*a, **k):
        ran.append(1)
        return {"generated": bt.now_ms(), "strategies": [], "errors": []}
    monkeypatch.setattr(bt, "run", fake_run)
    await b.step()
    assert len(bus.sent) == 1 and not ran                                # reciente: publica sin recalcular
    assert bt.now_ms() + 3600_000 < b.next_ms <= bt.now_ms() + bt.RETRY_S * 1000   # con errores, en 2 h y no en 20
    await b.step()
    assert len(bus.sent) == 1                                            # sin cambios: ni relee ni publica
