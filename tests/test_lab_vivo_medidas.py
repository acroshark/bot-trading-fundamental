"""Estadística del laboratorio vivo (docs/07, 8, 9 y 10.3): t HAC y p; Sharpe deflactado con N acumulado; BH con las
previas a p = 1; N efectivo; base emparejada con un sentido conocido; sin 5 mayores y sin la mejor semana; avance
trimestral con elección hacia atrás; clases de frecuencia; meta del 1 % (2 R al día = 1 %); Holm acumulado."""
import math

import numpy as np
import pytest

from bot.lab_vivo import criterios, medidas
from test_lab_vivo_comun import DAY, T0


def op(t, R, d=1, coin="BTC"):
    return {"t": t, "t_fill": t, "ext": t + 3_600_000, "dia": medidas.dia_de(t), "R_f": R, "R": R, "d": d, "coin": coin,
            "N": 30.0, "ret": R * 1.5, "fund_pct": 0.0, "sl": 1.5, "why": "objetivo" if R > 0 else "inicial"}


def test_p_hac_y_t():
    rng = np.random.default_rng(1)
    x = rng.normal(0.2, 1.0, 600)
    t, p = medidas.p_hac(x)
    assert t > 3 and p < 0.01
    t0, p0 = medidas.p_hac(rng.normal(0.0, 1.0, 600))
    assert p0 > 0.01
    assert medidas.p_hac(np.zeros(10)) == (0.0, 1.0)


def test_dsr_sube_el_liston_con_n():
    sr, T = 0.12, 440
    d1 = medidas.dsr(sr, T, 0.0, 3.0, 10)
    d2 = medidas.dsr(sr, T, 0.0, 3.0, 4000)
    assert d1 > d2
    assert medidas.dsr(0.5, T, 0.0, 3.0, 4000) > 0.95     # una ventaja real pasa (docs/07, 2.3)
    assert medidas.z_umbral(3500) == pytest.approx(3.60, abs=0.05)


def test_bh_con_previas_a_p_1():
    ps = [0.0001, 0.02] + [1.0] * 3917
    q = medidas.bh(ps)
    assert q[0] == pytest.approx(0.0001 * len(ps) / 1)
    assert q[1] > 0.10                                    # con 3.917 previas, un p de 0,02 no pasa


def test_n_efectivo():
    rng = np.random.default_rng(2)
    a = rng.normal(0, 1, 400)
    iguales = [a, a + 1e-9 * rng.normal(0, 1, 400), a * 2]
    assert medidas.n_efectivo(iguales) < 1.5
    indep = [rng.normal(0, 1, 400) for _ in range(5)]
    assert medidas.n_efectivo(indep) > 3.5


def test_base_dos_sentidos_con_sentido_conocido():
    rng = np.random.default_rng(3)
    ops = [op(T0 + k * DAY, float(rng.normal(0.8, 1)), d=1 if k % 2 else -1) for k in range(200)]
    opp = [-o["R_f"] - 0.1 for o in ops]
    b = medidas.base_dos_sentidos(ops, opp, 7)
    assert b["modo"] == "dos_sentidos" and b["p"] < 0.01
    b2 = medidas.base_dos_sentidos(ops, [o["R_f"] for o in ops], 7)
    assert b2["p"] > 0.2                                   # si el contrario da lo mismo, no hay ventaja de sentido


def test_base_un_sentido_con_fondo():
    from datetime import datetime
    ops = [op(T0 + k * DAY + 10 * 3_600_000, 0.5) for k in range(60)]
    fondo = {}
    for o in ops:
        dt = datetime.fromtimestamp(o["t"] / 1000, medidas.MAD)
        fondo[("BTC", dt.weekday(), dt.hour)] = [-0.2, 0.0, -0.1, 0.1]
    b = medidas.base_un_sentido(ops, fondo, 5)
    assert b["modo"] == "un_sentido" and b["p"] < 0.01
    assert medidas.modo_base(ops) == "un_sentido"


def test_sin_5_mayores_y_sin_mejor_semana():
    ops = [op(T0 + k * DAY, -0.2) for k in range(40)] + [op(T0 + 3 * DAY, 9.0) for _ in range(5)]
    assert medidas.media(ops) > 0
    assert medidas.sin_mayores(ops, 5) == pytest.approx(-0.2)
    R, sem = medidas.sin_mejor_semana(ops)
    assert R < 0 and sem == medidas.semana_iso(medidas.dia_de(T0 + 3 * DAY))


def test_avance_trimestral_elige_con_lo_anterior():
    corte = int(__import__("datetime").datetime(2025, 8, 16, tzinfo=medidas.MAD).timestamp() * 1000)
    fin = corte + 380 * DAY
    a = [op(corte - (200 - k) * DAY, 1.0) for k in range(100)] + [op(corte + k * 3 * DAY, -0.5) for k in range(120)]
    b = [op(corte - (200 - k) * DAY, 0.1) for k in range(100)] + [op(corte + k * 3 * DAY, 0.4) for k in range(120)]
    r = medidas.avance_trimestral({"A": a, "B": b}, corte, fin)
    assert r["trimestres"][0]["elegida"] == "A"            # lo mejor dentro (hacia atrás) se elige primero …
    assert not r["pasa"] or r["R"] > 0                     # … y se mide en el trimestre siguiente
    r2 = medidas.avance_trimestral({"B": b}, corte, fin)
    assert r2["pasa"] and r2["R"] == pytest.approx(0.4)


def test_mitades_semanas_y_frecuencia():
    ops = [op(T0 + k * DAY, 0.1 if k < 50 else -0.1) for k in range(100)]
    h1, h2 = medidas.mitades(ops, T0, T0 + 100 * DAY)
    assert h1 > 0 > h2
    assert medidas.semanas(ops) >= 14
    assert medidas.clase_frecuencia(1.2) == "frecuente"
    assert medidas.clase_frecuencia(0.3) == "selectiva"
    assert medidas.clase_frecuencia(0.2) is None


def test_meta_del_1_por_ciento():
    m = medidas.meta(2.0)
    assert m["pct_dia"] == pytest.approx(1.0) and m["falta_R_dia"] == pytest.approx(0.0)
    m2 = medidas.meta(0.29)
    assert m2["falta_R_dia"] == pytest.approx(1.71) and m2["falta_pct_dia"] == pytest.approx(0.855)
    assert "Le faltan 1,71 R al día" in m2["texto"] and "El riesgo no se sube" in m2["texto"]
    assert medidas.META_R == 2.0 and medidas.RISK == 0.5


def test_holm_acumulado():
    adj = medidas.holm([0.01, 0.04, 0.03, 0.5])
    assert adj[0] == pytest.approx(0.04) and adj[2] == pytest.approx(0.09) and adj[1] == pytest.approx(0.09)
    assert all(a <= 1 for a in adj)


def test_tramo_y_cuenta():
    ops = [op(T0 + k * DAY, 0.4 if k % 3 else -1.0) for k in range(90)]
    tm = medidas.tramo(ops, medidas.dia_de(T0), medidas.dia_de(T0 + 89 * DAY))
    assert tm["n"] == 90 and tm["dias"] == 90 and math.isclose(tm["op_dia"], 1.0)
    assert tm["R_dia"] == pytest.approx(sum(o["R_f"] for o in ops) / 90, abs=1e-4)
    assert tm["pct_dia"] == pytest.approx(tm["R_dia"] * 0.5, abs=1e-4)
    assert tm["ic95"][0] < tm["R"] < tm["ic95"][1]


def test_tramo_sin_las_5_mayores_por_dia():
    """Informativo en cada tramo (dentro, fuera y avance): lo mismo que B2, también en R al día y en % de la cuenta."""
    ops = [op(T0 + k * DAY, -0.2) for k in range(40)] + [op(T0 + 3 * DAY, 9.0) for _ in range(5)]
    tm = medidas.tramo(ops, medidas.dia_de(T0), medidas.dia_de(T0 + 39 * DAY))
    s5 = tm["sin_5_mayores"]
    assert s5["R"] == pytest.approx(medidas.sin_mayores(ops, 5)) == pytest.approx(-0.2)
    assert s5["R_dia"] == pytest.approx(-0.2 * 40 / 40) and s5["pct_dia"] == pytest.approx(s5["R_dia"] * 0.5)
    assert s5["suma_R_de_las_5"] == pytest.approx(45.0)
    assert tm["R_dia"] > 0 > s5["R_dia"]          # lo que gana depende de 5 operaciones
    assert medidas.tramo(ops[:5], medidas.dia_de(T0), medidas.dia_de(T0 + 4 * DAY))["sin_5_mayores"] is None


def test_cuenta_honesta():
    pv = {"A": {"2025-T3": {"n": 150, "suma_R": 30.0}, "2025-T4": {"n": 50, "suma_R": -10.0}},
          "B": {"2025-T3": {"n": 150, "suma_R": -10.0}, "2025-T4": {"n": 50, "suma_R": 5.0}}}
    corte = int(__import__("datetime").datetime(2025, 8, 16, tzinfo=medidas.MAD).timestamp() * 1000)
    r = medidas.cuenta_honesta(pv, corte, corte + 200 * DAY, top=1, min_n=100)
    assert r["trimestres"] and r["trimestres"][0]["trimestre"] == "2025-T4" and r["R_medio"] == pytest.approx(-0.2)


def test_criterios_fijados():
    assert criterios.VERSION == 1 and criterios.FECHA == "2026-10-29" and len(criterios.SHA) == 12
    assert criterios.c("rapida", "min_ops_dentro") == 120 and criterios.c("robustez", "dsr_min") == 0.95
