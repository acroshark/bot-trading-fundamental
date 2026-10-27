import io
import json
import math
import statistics
import types
import zipfile
from datetime import date, datetime, timezone

import numpy as np
import pytest

from bot import apertura, config
from bot import backtest_aperturas as bt
from bot import laboratorio as L

MIN = L.MIN
T0 = int(datetime(2025, 3, 3, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)


def dense(close, high=None, low=None, open_=None, vol=None, t0=T0):
    """Serie densa a partir de listas (precio constante si no se da otra cosa)."""
    c = np.asarray(close, float)
    n = len(c)
    o = np.asarray(open_ if open_ is not None else np.r_[c[0], c[:-1]], float)
    h = np.asarray(high if high is not None else np.maximum(o, c), float)
    l_ = np.asarray(low if low is not None else np.minimum(o, c), float)
    v = np.asarray(vol if vol is not None else np.full(n, 10.0), float)
    rows = np.column_stack([t0 + np.arange(n) * MIN, o, h, l_, c, v, v * c])
    return L.dense_from_rows(rows)


def one(S, j, d, price=None, lim=False, nxt=-1, sl=1.5, r=2.0, dur="24h"):
    """Una entrada única por passages y su horquilla resuelta (Lab.resolve sobre un objeto mínimo)."""
    P = np.array([S["o"][j] if price is None else price])
    U = L.passages(S, np.array([j]), np.array([d]), P, np.array([lim]), np.array([nxt]))
    res = L.Lab.resolve(types.SimpleNamespace(U=U), np.array([0]), sl, r, dur)
    return {k: v[0] for k, v in res.items()}, U


def net(d, entry, exit_, stop=False):
    xs = L.SLIP + (L.STOP_EXTRA if stop else 0.0)
    xf = exit_ * (1 - d * xs)
    return (d * (xf / entry - 1) - L.FEE * (1 + xf / entry)) * 100


# ---------------------------------------------------------------- primer paso

def test_primer_paso_igual_que_fuerza_bruta():
    rng = np.random.default_rng(7)
    n = 25000
    x = 100 * np.exp(np.cumsum(rng.normal(0, 0.002, n)))
    bx = L.block_max(x)
    s = rng.integers(0, n - 5, 300)
    end = np.minimum(s + rng.integers(1, 12000, 300), n)
    lv = np.array([1.0, 1.004, 1.02, 1.05, 1.3])
    k = L.first_cross(x, bx, s, 1 / x[s], lv, end)
    for i in range(len(s)):
        for q, t in enumerate(lv):
            seg = x[s[i]:end[i]] * (1 / x[s[i]]) >= t     # la misma aritmética (en el nivel 1,0 exacto importa)
            assert k[i, q] == (s[i] + int(seg.argmax()) if seg.any() else -1)


# ---------------------------------------------------------------- horquillas con costes

def test_largo_toca_objetivo_con_costes():
    c = np.full(3000, 100.0)
    c[1010:1030] = np.linspace(100, 104, 20)
    c[1030:] = 104
    S = dense(c)
    res, U = one(S, 1000, 1)
    E = 100 * (1 + L.SLIP)
    assert U["px"][0] == pytest.approx(E)
    assert res["why"] == 1
    assert res["ret"] == pytest.approx(net(1, E, E * 1.03))
    assert res["R"] == pytest.approx(res["ret"] / 1.5)
    # +3 % bruto en precio; neto algo menos de 2R por comisiones y deslizamiento
    assert 1.8 < res["R"] < 2.0
    k_tp = int(np.argmax(S["h"] >= E * 1.03 * (1 + L.TP_THROUGH)))   # tiene que superar el objetivo en 2 pb
    assert res["ext"] == T0 + (k_tp + 1) * MIN


def test_objetivo_tocado_justo_no_se_cobra():
    c = np.full(3000, 100.0)
    h = c.copy()
    E = 100 * (1 + L.SLIP)
    h[1010] = E * 1.03 * (1 + L.TP_THROUGH / 2)     # lo toca pero no lo supera en 2 pb
    S = dense(c, high=h)
    P = np.array([S["o"][1000]])
    U = L.passages(S, np.array([1000]), np.array([1]), P, np.array([False]), np.array([-1]))
    lab = types.SimpleNamespace(U=U)
    strict = L.Lab.resolve(lab, np.array([0]), 1.5, 2.0, "24h")
    touch = L.Lab.resolve(lab, np.array([0]), 1.5, 2.0, "24h", touch=True)
    assert strict["why"][0] == 2 and touch["why"][0] == 1     # con un simple toque sí contaría


def test_extra_de_stop_por_moneda():
    c = np.full(3000, 100.0)
    l_ = c.copy()
    l_[1003] = 97.0
    S = dense(c, low=l_)
    P = np.array([S["o"][1000]])
    U = L.passages(S, np.array([1000]), np.array([1]), P, np.array([False]), np.array([-1]))
    E = 100 * (1 + L.SLIP)
    base = L.Lab.resolve(types.SimpleNamespace(U=U), np.array([0]), 1.5, 2.0, "24h")
    U2 = dict(U, sx=np.array([L.STOP_EXTRA_ALT], np.float32))
    alt = L.Lab.resolve(types.SimpleNamespace(U=U2), np.array([0]), 1.5, 2.0, "24h")
    xf = lambda sx: E * 0.985 * (1 - (L.SLIP + sx))
    assert base["ret"][0] == pytest.approx((xf(L.STOP_EXTRA) / E - 1 - L.FEE * (1 + xf(L.STOP_EXTRA) / E)) * 100)
    assert alt["ret"][0] == pytest.approx((xf(L.STOP_EXTRA_ALT) / E - 1 - L.FEE * (1 + xf(L.STOP_EXTRA_ALT) / E)) * 100,
                                          rel=1e-5)
    assert alt["R"][0] < base["R"][0]


def test_stop_con_hueco_y_extra_de_stop():
    c = np.full(3000, 100.0)
    o = c.copy()
    o[1005] = 97.0                     # la vela abre por debajo del stop (98,5)
    c[1005:] = 97.0
    S = dense(c, open_=o)
    res, _ = one(S, 1000, 1)
    E = 100 * (1 + L.SLIP)
    assert res["why"] == 0
    assert res["ret"] == pytest.approx(net(1, E, 97.0, stop=True))   # sale a la apertura, no al stop
    assert res["R"] < -1.9


def test_misma_vela_toca_stop_y_objetivo_cuenta_stop():
    c = np.full(3000, 100.0)
    h, l_ = c.copy(), c.copy()
    h[1003], l_[1003] = 105.0, 97.0     # una vela enorme toca los dos
    S = dense(c, high=h, low=l_)
    res, _ = one(S, 1000, 1)
    E = 100 * (1 + L.SLIP)
    assert res["why"] == 0
    assert res["ret"] == pytest.approx(net(1, E, E * (1 - 0.015), stop=True))


def test_corto_simetrico():
    c = np.full(3000, 100.0)
    c[1010:1040] = np.linspace(100, 96, 30)
    c[1040:] = 96
    S = dense(c)
    res, _ = one(S, 1000, -1)
    E = 100 * (1 - L.SLIP)
    assert res["why"] == 1
    assert res["ret"] == pytest.approx(net(-1, E, E * 0.97))
    assert 1.8 < res["R"] < 2.0


def test_salida_por_tiempo_y_siguiente_apertura():
    c = np.full(6000, 100.0)
    c[1000:] = 100.5
    S = dense(c)
    res, U = one(S, 1000, 1, dur="24h")
    assert res["why"] == 2 and res["ext"] == T0 + (1000 + 1440) * MIN
    nxt = T0 + (1000 + 600) * MIN          # la misma apertura del día siguiente, 10 h después
    res2, _ = one(S, 1000, 1, nxt=nxt, dur="sig")
    assert res2["why"] == 2 and res2["ext"] == nxt
    E = S["o"][1000] * (1 + L.SLIP)
    assert res2["ret"] == pytest.approx(net(1, E, 100.5))
    res3, _ = one(S, 1000, 1, dur="7d")    # sin tope: 7 días o fin de datos
    assert res3["why"] == 3


def test_entrada_limite_la_vela_de_la_entrada_solo_cuenta_para_el_stop():
    c = np.full(3000, 100.0)
    h = c.copy()
    h[1000] = 104.0                        # el máximo de la vela del retesteo pudo ser antes de llenar
    S = dense(c, high=h)
    res, _ = one(S, 1000, 1, price=100.0, lim=True)
    assert res["why"] == 2                 # no cuenta como objetivo
    c2 = np.full(3000, 100.0)
    l2 = c2.copy()
    l2[1000] = 98.0                        # el mínimo de esa vela sí cuenta para el stop
    res2, _ = one(dense(c2, low=l2), 1000, 1, price=100.0, lim=True)
    assert res2["why"] == 0


def test_hueco_de_datos_descarta_la_operacion():
    n = 4000
    t = T0 + np.arange(n) * MIN
    keep = (np.arange(n) < 1100) | (np.arange(n) >= 1200)   # faltan 100 min seguidos
    c = np.full(n, 100.0)
    c[1300:] = 104.0
    rows = np.column_stack([t, c, c, c, c, np.full(n, 10.0), c * 10])[keep]
    S = L.dense_from_rows(rows)
    res, U = one(S, 1000, 1)
    assert U["gap"][0] == 100 and not res["ok"]


# ---------------------------------------------------------------- tabla de eventos sin mirar al futuro

def _synth_openings(d0: date, d1: date, only=None):
    ops = [o for o in bt.build_openings(d0, d1, {}) if o["id"] in L.WIN_IDS and (only is None or o["id"] in only)]
    return ops


def _synth_series(ops, seed=1, signs=None, start=None, days=None):
    rng = np.random.default_rng(seed)
    start = start or (ops[0]["open_ms"] // 86400_000 - 3) * 86400_000
    end = ops[-1]["open_ms"] + 8 * 86400_000
    n = (end - start) // MIN
    ret = rng.normal(0, 0.0004, n)
    if signs is not None:
        for o, s in zip(ops, signs):
            i = (o["open_ms"] - start) // MIN
            ret[i + 20:i + 200] += s * 0.06 / 180
    c = 100 * np.exp(np.cumsum(ret))
    o_ = np.r_[c[0], c[:-1]]
    hi = np.maximum(o_, c) * (1 + np.abs(rng.normal(0, 0.0002, n)))
    lo = np.minimum(o_, c) * (1 - np.abs(rng.normal(0, 0.0002, n)))
    v = rng.uniform(5, 15, n)
    rows = np.column_stack([start + np.arange(n) * MIN, o_, hi, lo, c, v, v * c])
    return rows


def test_tabla_de_eventos_no_mira_al_futuro():
    ops = _synth_openings(date(2025, 3, 3), date(2025, 4, 30))
    meta = L.opening_meta(ops)
    op = {k: meta[k] for k in ("open_ms", "next_same_ms", "prev_same_ms", "prev_ms", "prev2_ms", "prev_sess")}
    rows = _synth_series(ops, seed=3)
    S = L.dense_from_rows(rows)
    A = L.coin_tables(S, op)
    k = len(ops) - 12                       # un evento con historia previa
    ii = int((ops[k]["open_ms"] - S["t0"]) // MIN)
    cut = ii + 30                           # todo lo que pasa desde +30 min se cambia
    rows2 = rows.copy()
    rng = np.random.default_rng(99)
    sl = slice(cut, None)
    noise = np.exp(np.cumsum(rng.normal(0, 0.003, len(rows2) - cut)))
    for col in (1, 2, 3, 4):
        rows2[sl, col] = rows2[sl, col] * noise
    rows2[sl, 5] = rng.uniform(0, 50, len(rows2) - cut)
    B = L.coin_tables(L.dense_from_rows(rows2), op)
    for f in ("pre15", "pre60", "pre240", "mom15", "react_prev", "react_prev2", "desde_prev", "react_same", "atr_pct"):
        a, b = A["F"][f][k], B["F"][f][k]
        assert (np.isnan(a) and np.isnan(b)) or a == pytest.approx(b), f
    cut_ms = S["t0"] + cut * MIN
    checked = 0
    for key, ki in L.KEY_IDX.items():
        for di in (0, 1):
            da, db = A["slot_dec"][k, ki, di], B["slot_dec"][k, ki, di]
            if key[0] == "ret" and (da == L.NONE_T or db == L.NONE_T):
                continue                       # el retesteo se llena (o no) después de decidir: eso es futuro
            if da <= cut_ms or db <= cut_ms:   # decidida antes del cambio: idéntica en las dos
                assert da == db, key
                for name in ("slot_mom", "slot_vdev", "slot_rvol"):
                    x, y = A[name][k, ki, di], B[name][k, ki, di]
                    assert (np.isnan(x) and np.isnan(y)) or x == pytest.approx(y), (key, name)
                if key[0] != "ret":            # el retesteo se llena después de decidir
                    assert A["slot_t"][k, ki, di] == B["slot_t"][k, ki, di]
                checked += 1
    assert checked >= 6                        # al menos las entradas a +15 y +30 y alguna ruptura


def test_bolsas_previas_con_margen_de_cierre():
    # Miércoles 12-3-2025, apertura de Europa a las 08:00 UTC. Shanghái cierra a las 07:00 UTC (se conoce a las
    # 07:15) y Hong Kong a las 08:00 UTC (+15 min = 08:15 > 08:00: ese día aún no se conoce).
    day = date(2025, 3, 12)
    eu = [o for o in apertura.openings_on(day) if o["id"] == "europa"][0]
    idx = {"shanghai": {"close": {"2025-03-11": (100.0, 100.0), "2025-03-12": (100.5, 100.0)},
                        "first": "2025-01-01", "last": "2025-03-12"},
           "hongkong": {"close": {"2025-03-11": (99.0, 100.0), "2025-03-12": (105.0, 99.0)},
                        "first": "2025-01-01", "last": "2025-03-12"},
           "londres": {"close": {"2025-03-10": (100.0, 99.0), "2025-03-11": (101.0, 100.0)},
                       "first": "2025-01-01", "last": "2025-03-12"}}
    F = L.chain_table([eu], idx)
    assert F["c_ult"][0] == pytest.approx(0.5)          # Shanghái del mismo día, no Hong Kong
    assert F["c_asia"][0] == pytest.approx((0.5 - 1.0) / 2)   # Hong Kong del día anterior (−1 %), nunca +6 %
    assert F["own_prev"][0] == pytest.approx(1.0)       # Londres (bolsa de la ventana): sesión anterior
    assert F["own_streak"][0] == 2                      # dos subidas seguidas
    # Sin el dato de un día que ya pudo cerrar, no se usa nada de esa bolsa
    idx["shanghai"]["last"] = "2025-03-11"
    F2 = L.chain_table([eu], idx)
    assert F2["c_ult"][0] != pytest.approx(0.5)


# ---------------------------------------------------------------- gramática

def test_gramatica_reglas_sin_duplicados_y_todas_las_familias():
    sts = L.generate({"fng", "funding", "macro", "gdelt"}, 2000)
    assert len(sts) == 2000
    assert len({s["id"] for s in sts}) == 2000
    assert {s["family"] for s in sts} == set(L.FAMILIES)
    for s in sts:
        assert L.rules_ok(s)
        assert s["sl"] in L.SL_GRID and s["sl"] >= 1.5 and s["r"] in L.R_GRID and s["r"] >= 2
        assert s["entry"] in L.KEY_IDX and s["name"] and "SL" in s["name"]
        for nb in L.neighbors(s):
            assert nb["sl"] >= 1.5 and nb["r"] >= 2 and L.rules_ok(nb)
    exits = {(s["sl"], s["r"], s["dur"]) for s in sts}
    assert len(exits) == len(L.SL_GRID) * len(L.R_GRID) * len(L.DURS)
    assert [s["id"] for s in L.generate({"fng", "funding", "macro", "gdelt"}, 2000)] == [s["id"] for s in sts]
    base = L.generate(set(), 2000)
    assert len(base) == 2000
    assert not {s["family"] for s in base} & {"miedo_codicia", "funding", "macro", "gdelt"}


# ---------------------------------------------------------------- estadística

def test_benjamini_hochberg():
    q = L.bh_qvalues([0.01, 0.04, 0.03, 0.005, float("nan")])
    assert q[:4] == pytest.approx([0.025, 0.05, 0.05, 0.025])
    assert q[4] == 1.0


def test_t_de_student():
    assert L.t_sf(0.0, 10) == pytest.approx(0.5)
    assert L.t_sf(2.228, 10) == pytest.approx(0.025, abs=2e-4)     # tabla: t0,975 con 10 gl
    assert L.t_sf(1.96, 10_000) == pytest.approx(0.025, abs=2e-4)
    assert L.t_sf(-2.228, 10) == pytest.approx(0.975, abs=2e-4)


def test_sharpe_deflactado():
    nd = statistics.NormalDist()
    sr, T = 0.1, 500
    psr = nd.cdf(sr * math.sqrt(T - 1) / math.sqrt(1 - 0 * sr + (3 - 1) / 4 * sr * sr))
    assert L.deflated_sharpe(sr, T, 0.0, 3.0, 0.01, 1) == pytest.approx(psr)
    vals = [L.deflated_sharpe(sr, T, 0.0, 3.0, 0.0025, n) for n in (2, 10, 100, 2000)]
    assert all(a > b for a, b in zip(vals, vals[1:]))
    v, n = 0.0025, 100
    sr0 = math.sqrt(v) * ((1 - L.EULER) * nd.inv_cdf(1 - 1 / n) + L.EULER * nd.inv_cdf(1 - 1 / (n * math.e)))
    assert L.deflated_sharpe(sr0, T, 0.0, 3.0, v, n) == pytest.approx(0.5)


def test_sharpe_deflactado_con_el_t_de_cada_estrategia():
    # Con la varianza nula propia, el umbral es Z(N)/√(T − 1): con más datos se exige un Sharpe menor (no mayor).
    z = L.expected_max_z(300)
    assert 2.5 < z < 3.2
    d_short = L.deflated_sharpe(0.6, 31, 0.0, 3.0, None, 300)
    d_long = L.deflated_sharpe(0.3, 151, 0.0, 3.0, None, 300)
    t_s, t_l = 0.6 * math.sqrt(30) / math.sqrt(1 + 0.5 * 0.36), 0.3 * math.sqrt(150) / math.sqrt(1 + 0.5 * 0.09)
    nd = statistics.NormalDist()
    assert d_short == pytest.approx(nd.cdf(t_s - z)) and d_long == pytest.approx(nd.cdf(t_l - z))
    assert L.deflated_sharpe(0.2, 100, 0.0, 3.0, None, 1) == pytest.approx(nd.cdf(0.2 * math.sqrt(99) /
                                                                                   math.sqrt(1 + 0.5 * 0.04)))


def test_t_con_autocorrelacion_y_pruebas_efectivas():
    rng = np.random.default_rng(3)
    x = rng.normal(0.1, 1, 4000)
    t_iid = x.mean() / (x.std(ddof=1) / math.sqrt(len(x)))
    assert L.hac_t(x) == pytest.approx(t_iid, rel=0.1)
    y = np.repeat(rng.normal(0.1, 1, 1000), 4)          # cada dato se repite 4 días: el t simple se infla ×2
    t_y = y.mean() / (y.std(ddof=1) / math.sqrt(len(y)))
    assert L.hac_t(y) < 0.7 * t_y
    base = rng.normal(0, 1, (1, 300))
    same = np.repeat(base, 50, 0) + rng.normal(0, 0.01, (50, 300))      # 50 copias casi iguales: ~1 prueba
    assert L.effective_trials(same) < 3
    indep = rng.normal(0, 1, (50, 300))                                   # 50 independientes: ~50
    assert 35 < L.effective_trials(indep) <= 50.5


def test_cuenta_desempata_por_prioridad_y_no_por_salida():
    lab = types.SimpleNamespace(days=["2025-03-03", "2025-03-04"], split_day="2025-03-03",
                                oos=np.array([True, True]), coin=np.array([0, 0]))
    t = int(datetime(2025, 3, 3, 9, tzinfo=timezone.utc).timestamp() * 1000)
    a = {"e": np.array([0]), "ent": np.array([t]), "ext": np.array([t + 5 * 3600_000]), "ret": np.array([3.0])}
    b = {"e": np.array([1]), "ent": np.array([t]), "ext": np.array([t + 3600_000]), "ret": np.array([-1.5])}
    acc = L.account(lab, [a, b], [1.5, 1.5], 0.5, 1.0)       # entran a la vez en la misma moneda: manda «a» (primera)
    assert acc["final_equity"] == pytest.approx(10_000 + 10_000 * 0.5 / 1.5 * 0.03)
    acc2 = L.account(lab, [b, a], [1.5, 1.5], 0.5, 1.0)      # con el otro orden, «b», aunque salga antes o después
    assert acc2["final_equity"] == pytest.approx(10_000 - 10_000 * 0.5 / 1.5 * 0.015)


def test_miercoles_de_ceniza_de_b3():
    assert [L.ash_wednesday(y).isoformat() for y in (2024, 2025, 2026)] == ["2024-02-14", "2025-03-05", "2026-02-18"]
    assert L.special_session({"id": "saopaulo", "day": "2025-03-05"})
    assert not L.special_session({"id": "nuevayork", "day": "2025-03-05"})
    assert not L.special_session({"id": "saopaulo", "day": "2025-03-06"})


def test_macro_en_las_franjas_de_apertura():
    om = np.array([T0, T0 + 86400_000])
    macro = np.array([T0 - 2 * 3600_000, T0 + 86400_000 + 5 * 3600_000], np.int64)   # el segundo cae fuera
    names = {int(macro[0]): "CPI", int(macro[1]): "FOMC"}
    assert L.macro_in_windows(om, macro, names) == {"CPI": 1, "FOMC": 0}


def test_una_posicion_por_moneda():
    coin = np.array([0, 0, 0, 1, 0])
    ent = np.array([0, 5, 10, 6, 20])
    ext = np.array([10, 8, 15, 7, 25])
    keep = L.no_overlap(coin, ent, ext, np.arange(5))
    assert keep.tolist() == [True, False, True, True, True]   # la que entra justo al salir la otra sí entra


# ---------------------------------------------------------------- laboratorio sintético: base, permutación y cuenta

@pytest.fixture(scope="module")
def synth_lab():
    ops = _synth_openings(date(2025, 1, 6), date(2025, 7, 31), only={"nuevayork"})
    rng = np.random.default_rng(5)
    signs = rng.choice([-1, 1], len(ops))
    rows = _synth_series(ops, seed=11, signs=signs)
    meta = L.opening_meta(ops)
    op = {k: meta[k] for k in ("open_ms", "next_same_ms", "prev_same_ms", "prev_ms", "prev2_ms", "prev_sess")}
    T = L.coin_tables(L.dense_from_rows(rows), op)
    feats = {"c_ult": signs.astype(float) * 1.0,                       # señal que acierta siempre
             "c_ny": rng.choice([-1.0, 1.0], len(ops))}                # señal sin relación
    split_ms, _ = L.split_point(meta["open_ms"])
    return L.Lab(meta, feats, {"BTC": T}, split_ms, coins=["BTC"])


def _st(**kw):
    st = {"family": "cadena", "src": ("cad_ult", 0.0), "conf": ("ninguna",), "vol": "todos", "btc": "ninguno",
          "win": "todas", "coins": "todas", "dia": "todos", "entry": ("t", 30), "sl": 1.5, "r": 2.0, "dur": "24h"}
    st.update(kw)
    st["id"] = L.skey(st)
    return st


def test_permutacion_y_base_aleatoria_detectan_la_senal(synth_lab):
    lab = synth_lab
    good, bad = _st(), _st(src=("cad_ny", 0.0))
    tg = lab.trades(good)
    assert len(tg["R"]) > 50 and tg["R"].mean() > 1.0              # casi siempre objetivo
    pg = L.perm_test(lab, good, 200)
    pb = L.perm_test(lab, bad, 200)
    assert pg["p"] <= 0.01
    assert pb["p"] > 0.05
    m = L.metrics(lab, tg, lab.oos[tg["e"]])
    pool = L.null_pool(lab, ("todas", "todas", ("t", 30), 1.5, 2.0, "24h"), 100)
    ns = L.null_stats(pool, m["trades"], 1)
    assert m["mean_R"] > ns["p95"]                                  # bate a la base aleatoria
    assert abs(ns["mean"]) < 0.6


def test_base_emparejada_con_los_mismos_eventos(synth_lab):
    lab = synth_lab
    good, bad = _st(), _st(src=("cad_ny", 0.0))
    nb_g = L.matched_base(lab, good, 60)
    nb_b = L.matched_base(lab, bad, 60)
    assert nb_g["beats"] and nb_g["p"] <= 0.05
    assert nb_g["beta"]["reps"] == 60 and nb_g["50"]["reps"] == 60
    assert nb_g["actual_mean_R"] > nb_g["p95_max"] >= max(nb_g["beta"]["p95"], nb_g["50"]["p95"])
    assert not nb_b["beats"]
    # la base usa los mismos eventos que la estrategia: con la dirección real, el resultado es el de la estrategia
    e, dd = lab.events(good, oos_only=True)
    assert len(lab.run(good, e, dd)["R"]) == nb_g["n"]


def test_permutacion_por_bloques_conserva_el_sentido_de_cada_apertura(synth_lab):
    lab = synth_lab
    st = _st()
    e, dd = lab.events(st, oos_only=True)
    uo, inv, mo, rel = L.opening_blocks(lab, e, dd)
    assert (rel * mo[inv] == dd).all() and len(uo) == len(np.unique(e // lab.nc))
    p = L.perm_test(lab, st, 50)
    assert p["applicable"] and p["p_min"] == pytest.approx(1 / 51, abs=1e-4) and "p_event" in p
    pb = L.perm_test(lab, _st(src=("ap_mom", 15, 0.2), entry=("pb", 15)), 50)
    assert pb["p"] is None and not pb["applicable"]          # con «pb» la permutación no informa


def test_metricas_nuevas_y_meses(synth_lab):
    lab = synth_lab
    tr = lab.trades(_st())
    m = L.metrics(lab, tr, lab.oos[tr["e"]], part="oos")
    for k in ("weeks", "long_share", "mean_R_wo_best_month", "best_month", "t_day_iid"):
        assert k in m
    assert 0 < m["long_share"] < 1 and m["weeks"] >= 5 and m["mean_R_wo_best_month"] > 0
    b = L.month_bootstrap(lab, tr, 500, seed=1)
    assert b["months"] >= 2 and b["p_le_0"] == 0.0
    # los cortos en perpetuo cobran el funding positivo
    t_all = np.array([lab.t_from + k * 8 * 3600_000 for k in range(int((lab.t_to - lab.t_from) / (8 * 3600_000)))])
    lab.funding = {0: np.column_stack([t_all, np.full(len(t_all), 0.0001)])}
    try:
        sh = L.short_side(lab, _st(), tr)
    finally:
        lab.funding = {}
    assert sh["trades"] > 0 and sh["funding_R"] > 0 and not sh["executable"]
    assert sh["mean_R_perp"] > sh["mean_R_spot_model"]
    lo = lab.trades(_st(), side=1)
    assert len(lo["R"]) and (lo["dir"] > 0).all()


def test_fin_de_datos_no_cuenta():
    ops = _synth_openings(date(2025, 1, 6), date(2025, 4, 30), only={"nuevayork"})
    rows = _synth_series(ops, seed=4)
    rows = rows[rows[:, 0] < ops[-1]["open_ms"] + 2 * 86400_000]        # los datos se acaban 2 días después
    meta = L.opening_meta(ops)
    op = {k: meta[k] for k in ("open_ms", "next_same_ms", "prev_same_ms", "prev_ms", "prev2_ms", "prev_sess")}
    T = L.coin_tables(L.dense_from_rows(rows), op)
    split_ms, _ = L.split_point(meta["open_ms"])
    lab = L.Lab(meta, {"c_ult": np.ones(len(ops))}, {"BTC": T}, split_ms, coins=["BTC"])
    st = _st(sl=3.0, r=3.0, dur="7d")
    e, dd = lab.events(st)
    e2, dd2, u = lab.candidates(st, e, dd)
    raw = lab.resolve(u, 3.0, 3.0, "7d")
    assert (raw["why"] == 3).any()                     # alguna se queda abierta al acabarse los datos…
    assert not (lab.trades(st)["why"] == 3).any()      # …y no cuenta como resultado


def test_comparacion_con_otra_pasada(tmp_path):
    other = {"period": {"from": "2021-01-04", "split": "2024-06-29", "desde": "2021-01"}, "version": 1,
             "all": [{"id": "a", "is": {"trades": 100, "mean_R": -0.2}, "oos": {"trades": 50, "mean_R": 0.1}},
                     {"id": "b", "is": {"trades": 100, "mean_R": 0.1}, "oos": {"trades": 50, "mean_R": -0.1}}],
             "promising": [{"id": "b"}],
             "walk_forward": {"min100": [{"year": 2026, "test_mean_R": -0.127}],
                              "account": {"riesgo_0_5_lev1": {"days": 1252, "total_pct": -62.0}}}}
    p = tmp_path / "otra.json"
    p.write_text(json.dumps(other), encoding="utf-8")
    rep = {"period": {"from": "2023-11-01"}, "all": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
           "promising": [{"id": "a"}, {"id": "c"}], "top50": [{"id": "a"}]}
    c = L.compare_run(p, rep)
    assert c["same_strategies"] == 2 and c["promising_both"] == 0 and c["promising_other"] == 1
    g = c["current_promising_there"]
    assert g["n"] == 2 and g["found"] == 1 and g["median_is_R"] == pytest.approx(-0.2) and g["positive_is"] == 0
    assert c["wf_min100_by_year"]["2026"] == pytest.approx(-0.127) and c["honest_account_0_5"]["total_pct"] == -62.0
    assert L.compare_run(tmp_path / "no.json", rep) is None


def test_clasificacion_y_cuenta(synth_lab):
    lab = synth_lab
    sts = [_st(), _st(src=("cad_ny", 0.0)), _st(sl=2.0), _st(r=2.5)]
    res = L.evaluate_all(lab, sts, log_every=0)
    L.classify(lab, res, 50, 100, n_trials=len(sts))
    assert res[0]["pos_oos"] and res[0].get("beats_base") and res[0]["checks"]["permutacion"]
    assert res[1]["class"] != "robusta"
    tr = lab.trades(sts[0])
    acc = L.account(lab, [tr], [1.5], 0.5, 1.0, part="all")
    assert acc["days"] == len(lab.days) and acc["final_equity"] > 10_000
    assert acc["mean_pct"] > 0
    acc2 = L.account(lab, [tr], [1.5], 0.5, 2.0, part="all")
    assert acc2["final_equity"] - 10_000 > acc["final_equity"] - 10_000   # 2:1 amplifica (aquí, ganancias)


def test_seleccion_en_avance_no_mira_el_año_que_prueba():
    lab = types.SimpleNamespace(year=np.array([2021, 2022, 2022]))
    mk = lambda sid, y1, y2: {"st": {"id": sid, "name": sid},
                              "by_year": {"2021": {"n": 100, "sum_R": y1 * 100, "mean_R": y1},
                                          "2022": {"n": 50, "sum_R": y2 * 50, "mean_R": y2}}}
    res = [mk("a", 0.5, -0.2), mk("b", -0.1, 0.9)]
    wf = L.walk_forward(lab, res, top=1, min_n=30)
    assert len(wf) == 1 and wf[0]["year"] == 2022
    assert wf[0]["top1"]["id"] == "a"              # se elige con 2021, aunque «b» sea la mejor en 2022
    assert wf[0]["test_mean_R"] == pytest.approx(-0.2)


def test_cuenta_a_mano():
    lab = types.SimpleNamespace(days=["2025-03-03", "2025-03-04"], split_day="2025-03-03",
                                oos=np.array([True]), coin=np.array([0]))
    t_ent = int(datetime(2025, 3, 3, 9, tzinfo=timezone.utc).timestamp() * 1000)
    tr = {"e": np.array([0]), "ent": np.array([t_ent]), "ext": np.array([t_ent + 3600_000]), "ret": np.array([3.0])}
    acc = L.account(lab, [tr], [1.5], 0.5, 1.0)
    assert acc["final_equity"] == pytest.approx(10_000 + 10_000 * 0.5 / 1.5 * 0.03)
    assert acc["worst_pct"] == pytest.approx(0.0) and acc["best_pct"] == pytest.approx(1.0)
    acc15 = L.account(lab, [tr], [1.5], 1.5, 1.0)                  # 1,5 % de riesgo con stop 1,5 % = nominal 100 %
    assert acc15["final_equity"] == pytest.approx(10_300)


# ---------------------------------------------------------------- datos y CLI

def test_zip_microsegundos_cabecera_y_huecos(tmp_path):
    lines = ["open_time,open,high,low,close,volume,close_time,quote_volume,count,tb,tq,ignore"]
    for k in (0, 1, 3):                   # falta el minuto 2
        t = (T0 + k * MIN) * 1000
        lines.append(f"{t},{100 + k},{101 + k},{99 + k},{100.5 + k},10,{t + 59_999_999},{1000 + k},1,5,0,0")
    p = tmp_path / "BTCUSDT-1m-2025-03.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("x.csv", "\n".join(lines) + "\n")
    a = L.parse_zip(p)
    assert a.shape == (3, 7) and a[0, 0] == T0 and a[2, 6] == 1003
    S = L.dense_from_rows(a)
    assert S["n"] == 4 and S["valid"].tolist() == [True, True, False, True]
    assert S["c"][2] == S["c"][1] and S["v"][2] == 0
    (tmp_path / "BTCUSDT-1m-2025-03-api.json").write_text(json.dumps({"rows": []}), encoding="utf-8")
    (tmp_path / "BTCUSDT-1m-2025-04-api.json").write_text(json.dumps({"rows": []}), encoding="utf-8")
    fs = L.month_files("BTC", folder=tmp_path)
    assert [f.name for f in fs] == ["BTCUSDT-1m-2025-03.zip", "BTCUSDT-1m-2025-04-api.json"]
    assert [f.name for f in L.month_files("BTC", desde="2025-04", folder=tmp_path)] == ["BTCUSDT-1m-2025-04-api.json"]


def test_fuentes_opcionales_sin_mirar_al_futuro(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HIST_DIR", tmp_path)
    (tmp_path / "sentimiento").mkdir()
    (tmp_path / "sentimiento" / "fng.json").write_text(json.dumps([["2025-03-02", 20], ["2025-03-03", 80]]))
    (tmp_path / "funding").mkdir()
    fr = [[T0 - k * 8 * 3600_000, 0.0001 * (k % 5)] for k in range(120)][::-1] + [[T0 + 3600_000, 0.01]]
    (tmp_path / "funding" / "BTCUSDT.json").write_text(json.dumps(fr))
    (tmp_path / "macro").mkdir()
    (tmp_path / "macro" / "eventos.json").write_text(json.dumps([{"name": "CPI", "date": "2025-03-03", "t_ms": T0}]))
    (tmp_path / "gdelt").mkdir()
    ser = [[T0 - k * 3600_000, 1.0 if k > 30 else 3.0, 5] for k in range(800)][::-1]
    (tmp_path / "gdelt" / "bitcoin.json").write_text(json.dumps({"query": "bitcoin", "resolution": "1h",
                                                                  "series": ser}))
    # la misma consulta en otra resolución (menos tiempo cubierto): no cuenta dos veces
    ser_d = [[T0 - k * 86400_000, -5.0, 5] for k in range(20)][::-1]
    (tmp_path / "gdelt" / "bitcoin-1d.json").write_text(json.dumps({"query": "bitcoin", "resolution": "1d",
                                                                     "series": ser_d}))
    fng = L.load_fng()
    f = L.fng_features(np.array([T0, T0 + 2 * 3600_000]), fng)
    assert f["fng"].tolist() == [20, 80]          # a las 00:00 UTC aún vale el del día anterior
    assert np.isnan(f["fng_d1"][0]) and f["fng_d1"][1] == 60
    fu = L.funding_features(np.array([T0]), L.load_funding("BTC"))
    assert fu[0] == 0.0                           # la de las 00:00 (0) es la mínima; la de las 01:00 aún no se sabe
    assert L.load_macro().tolist() == [T0]
    gd = L.load_gdelt()
    assert len(gd) == 1 and gd[0]["step"] == 3600_000 and gd[0]["lag"] == 3600_000
    z = L.gdelt_features(np.array([T0]), gd)
    assert z[0] > 1                               # el tono de las últimas 24 h está por encima de su media
    # sin mirar al futuro: un tramo horario solo se conoce al cerrar más 1 h de publicación
    t_ = np.array([T0 - k * 3600_000 for k in range(40)][::-1], np.int64)
    tone = np.r_[np.arange(39) % 2, 50.0]
    full = [{"t": t_, "step": 3600_000, "lag": 3600_000, "tone": tone}]
    cut = [{"t": t_[:-1], "step": 3600_000, "lag": 3600_000, "tone": tone[:-1]}]
    f = lambda t, s: L.gdelt_features(np.array([t]), s)[0]
    assert f(T0 + 3600_000, full) == pytest.approx(f(T0 + 3600_000, cut))       # el último tramo aún no se sabe
    assert f(T0 + 2 * 3600_000, full) != pytest.approx(f(T0 + 2 * 3600_000, cut))
    ts, names = L.load_macro(with_names=True)
    assert ts.tolist() == [T0] and names == {T0: "CPI"}


def test_salida_protege_los_archivos_del_motor(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BACKTEST_DIR", tmp_path)
    for name in ("aperturas.json", "resultados.json"):
        with pytest.raises(SystemExit):
            L.out_path(name)
    assert L.out_path("../otra/carpeta/lab.json") == tmp_path / "lab.json"


def test_pasada_completa_sintetica(tmp_path, monkeypatch):
    """CLI de punta a punta con dos monedas sintéticas y sin red: escribe el JSON con todas sus partes."""
    hist, bdir = tmp_path / "hist", tmp_path / "bt"
    monkeypatch.setattr(config, "HIST_DIR", hist)
    monkeypatch.setattr(config, "BACKTEST_DIR", bdir)
    ops = _synth_openings(date(2025, 1, 6), date(2025, 4, 30))
    start = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    for seed, coin in ((1, "BTC"), (2, "ETH")):
        rows = _synth_series(ops, seed=seed, start=start)
        sym = bt.SYMS[coin]
        for ym in ("2025-01", "2025-02", "2025-03", "2025-04", "2025-05"):
            a, b = bt.month_bounds(ym)
            part = rows[(rows[:, 0] >= a) & (rows[:, 0] < b)]
            if not len(part):
                continue
            buf = io.StringIO()
            for r in part:
                buf.write(f"{int(r[0])},{r[1]:.6f},{r[2]:.6f},{r[3]:.6f},{r[4]:.6f},{r[5]:.4f},{int(r[0]) + 59999},"
                          f"{r[6]:.4f},1,{r[5] / 2:.4f},0,0\n")
            d = hist / "1m"
            d.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(d / f"{sym}-1m-{ym}.zip", "w") as z:
                z.writestr(f"{sym}-1m-{ym}.csv", buf.getvalue())
    (hist / "sentimiento").mkdir()                 # solo cubre 10 días: no basta para probar su familia
    (hist / "sentimiento" / "fng.json").write_text(json.dumps([[f"2025-01-{d:02d}", 50] for d in range(1, 11)]))
    (hist / "macro").mkdir()                       # cubre todo el periodo: sí se prueba
    ev = [{"name": "CPI", "date": o["day"], "t_ms": o["open_ms"] - 3600_000} for o in ops if o["id"] == "nuevayork"]
    (hist / "macro" / "eventos.json").write_text(json.dumps(ev[::5]))
    rep = L.run_lab(None, 1, 60, n_null=10, n_perm=10, today=date(2025, 5, 3))
    assert rep["summary"]["tested"] == 60
    assert set(rep["data"]["coins"]) == {"BTC", "ETH"}
    assert rep["data"]["families_skipped"] == ["miedo_codicia", "funding", "gdelt"]
    assert rep["data"]["sources"]["coverage_of_period"]["fng"] < 0.5
    assert any(x["family"] == "macro" for x in rep["all"])
    for k in ("baseline", "top50", "robust", "promising", "families", "exits", "walk_forward", "verdict", "ceiling",
              "frozen"):
        assert k in rep
    assert rep["frozen"]["date"] == "2025-05-03" and rep["frozen"]["strategies"] == 60
    assert len(rep["frozen"]["strategies_sha256"]) == 64 and rep["frozen"]["criteria"]["min_weeks_oos"] == L.MIN_WEEKS_OOS
    assert "quarterly" in rep["walk_forward"] and "account_quarter" in rep["walk_forward"]
    assert rep["params"]["stop_extra_bps"]["BTC"] == 5.0 and rep["params"]["stop_extra_bps"]["SOL"] == 20.0
    assert "regime" in rep["data"] and set(rep["data"]["regime"]) == {"BTC", "ETH"}
    assert rep["data"]["sources"]["macro"]["in_windows"]["CPI"] > 0
    assert all("sensitivity" in x for x in rep["top50"])
    assert rep["summary"]["base_tested"] == rep["stats"]["base_tested"] == rep["summary"]["enough_trades"]
    assert all(x["rules"]["sl_pct"] >= 1.5 and x["rules"]["r"] >= 2 and not x["rules"]["break_even"]
               for x in rep["top50"])
    json.dumps(rep, allow_nan=False)


def test_cli_escribe_en_backtest_dir(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "BACKTEST_DIR", tmp_path)
    seen = {}

    def fake(desde, procs, max_n, n_null, n_perm, stop_extra=None, compare=None):
        seen.update(desde=desde, procs=procs, max_n=max_n, n_null=n_null, n_perm=n_perm, stop_extra=stop_extra,
                    compare=compare)
        return {"period": {"from": "2021-01-04", "to": "2026-10-23", "split": "2024-06-01"}, "summary": {"tested": 0},
                "top50": [], "verdict": ["Ninguna estrategia pasa todas las pruebas."],
                "frozen": {"date": "2026-10-26", "strategies": 0}}

    monkeypatch.setattr(L, "run_lab", fake)
    L.main(["--desde", "2023-11", "--procesos", "2", "--max", "10", "--salida", "lab.json", "--azar", "7",
            "--permutaciones", "9", "--slip-stop-alt", "30", "--comparar", "otra.json"])
    assert {k: seen[k] for k in ("desde", "procs", "max_n", "n_null", "n_perm")} == \
        {"desde": "2023-11", "procs": 2, "max_n": 10, "n_null": 7, "n_perm": 9}
    assert seen["stop_extra"]["SOL"] == pytest.approx(30 / 1e4) and seen["stop_extra"]["BTC"] == pytest.approx(L.STOP_EXTRA)
    assert seen["compare"] == tmp_path / "otra.json"
    assert json.loads((tmp_path / "lab.json").read_text(encoding="utf-8"))["summary"] == {"tested": 0}
    assert "Ninguna estrategia" in capsys.readouterr().out
    # la lista congelada no pisa la de otra pasada del mismo día
    assert (tmp_path / "lab-congelado-2026-10-26.json").exists()
    L.main(["--salida", "lab.json", "--max", "10", "--comparar", ""])
    assert (tmp_path / "lab-congelado-2026-10-26-2.json").exists() and seen["compare"] is None
