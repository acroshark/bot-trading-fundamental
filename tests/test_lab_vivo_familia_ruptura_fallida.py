"""Familia ruptura_fallida (Turtle Soup en velas de 1 h): con velas hechas a mano, la señal sale en el minuto, el sentido y
con el stop esperados, solo cuando la barrida de un nivel VIEJO falla a tiempo, y pasa la prueba del recorte con un
corte en cada señal. Sin red."""
import numpy as np
import pytest

from bot.lab_vivo import datos, filtros, recorte
from bot.lab_vivo.familias import obtener
from bot.lab_vivo.familias.ruptura_fallida import extremo_previo
from test_lab_vivo_comun import DAY, H, MIN, T0, reglas, velas_mano  # noqa: F401

R = 0.0005           # el rango de velas_mano: máximo y mínimo a ± 0,05 % del cuerpo
FAM = obtener("ruptura_fallida")


def minuto(dia: int, hora: int, mi: int = 0) -> int:
    return (dia * 24 + hora) * 60 + mi


def recorte_en_cada_senal(fam, m, p, a, b):
    s = fam.senales(m, p, a, b)
    assert len(s)
    r = recorte.comprobar(fam, [(m, a, b)], p, np.random.default_rng(1), 10, por_senal=len(s))
    assert not recorte.motivos(r) and r["cortes_senal"] == len(np.unique(s.t)), r
    return s


def plano(dias: int, nivel: float = 100.0) -> np.ndarray:
    return np.full(dias * 1440, nivel)


def test_extremo_previo_valor_y_antiguedad():
    x = np.array([5.0, 3.0, 4.0, 3.0, 6.0, 2.0])
    lo, e_lo = extremo_previo(x, 3, False)
    hi, e_hi = extremo_previo(x, 3, True)
    assert np.isnan(lo[:3]).all() and (e_lo[:3] == 0).all()
    assert lo[3:].tolist() == [3.0, 3.0, 3.0] and e_lo[3:].tolist() == [2, 3, 2]   # con empates, el más antiguo
    assert hi[3:].tolist() == [5.0, 4.0, 6.0] and e_hi[3:].tolist() == [3, 2, 1]
    assert not set(FAM.parametros) & set(filtros.PARAMS)                          # ningún parámetro tapa un filtro común


def test_barrida_de_un_minimo_viejo_que_vuelve_en_la_misma_vela(reglas):  # noqa: F811
    c = plano(4)
    c[minuto(1, 20):minuto(1, 21)] = 99.0      # el mínimo viejo (día 1, 20:00; aún sin 48 h detrás: no es señal)
    c[minuto(3, 10):minuto(3, 10, 30)] = 98.5  # la barrida, día 3 a las 10:00, y a las 10:30 vuelve a 100
    m = datos.Mercado(velas_mano(c))
    p = {"monedas": ["BTC"]}
    s = recorte_en_cada_senal(FAM, m, p, T0, T0 + len(c) * MIN)
    assert len(s) == 1 and s.dir[0] == 1 and s.moneda[0] == 0
    assert s.t[0] == T0 + 3 * DAY + 11 * H                   # el cierre de la vela de 10:00, que acaba en 100
    sl = (100 - 98.5 * (1 - R)) / 100 * 100 + 0.1
    assert s.stop_pct[0] == pytest.approx(sl, rel=1e-9)
    assert s.objetivo_r[0] == pytest.approx(100 * R / max(1.5, sl), rel=1e-9)   # el máximo de la ventana: 100 × (1 + R)
    assert s.prioridad[0] == pytest.approx(38 / 48)          # el nivel tiene 38 de las 48 velas de la ventana
    # las cuatro monedas (velas_mano les da el mismo camino) a la vez
    s4 = FAM.senales(m, {}, T0, T0 + len(c) * MIN)
    assert sorted(s4.moneda.tolist()) == [0, 1, 2, 3] and set(s4.t.tolist()) == {int(s.t[0])}
    # con una ventana de 24 h: el mínimo del día 1 ya tiene 24 h planas detrás, así que él mismo barre ese mínimo plano
    # (y vuelve dentro en la vela de 21:00); el día 3 su ventana ya no lo ve y la barrida del plano también le vale
    s24 = FAM.senales(m, {**p, "horas": 24}, T0, T0 + len(c) * MIN)
    assert s24.t.tolist() == [T0 + DAY + 22 * H, int(s.t[0])]
    assert s24.objetivo_r[1] == pytest.approx(s.objetivo_r[0], rel=1e-9)


def test_vuelve_dentro_mas_tarde_segun_velas_max(reglas):  # noqa: F811
    c = plano(4)
    c[minuto(1, 20):minuto(1, 21)] = 99.0
    c[minuto(3, 10):minuto(3, 11, 30)] = 98.5  # la vela de 10:00 cierra fuera; la de 11:00 cierra otra vez dentro
    m = datos.Mercado(velas_mano(c))
    p = {"monedas": ["BTC"], "velas_max": 3}
    s = recorte_en_cada_senal(FAM, m, p, T0, T0 + len(c) * MIN)
    assert len(s) == 1 and s.dir[0] == 1 and s.t[0] == T0 + 3 * DAY + 12 * H
    assert s.stop_pct[0] == pytest.approx((100 - 98.5 * (1 - R)) + 0.1, rel=1e-9)
    assert not len(FAM.senales(m, {**p, "velas_max": 1}, T0, T0 + len(c) * MIN))   # con 1 vela, la barrida no falló


def test_un_nivel_reciente_no_vale(reglas):  # noqa: F811
    c = plano(4)
    c[minuto(3, 5):minuto(3, 6)] = 99.0        # día 3 a las 05:00: barre el mínimo plano (viejo) y vuelve a las 06:00
    c[minuto(3, 10):minuto(3, 10, 30)] = 98.5  # a las 10:00 barre el de las 05:00, que solo tiene 5 velas: no
    m = datos.Mercado(velas_mano(c))
    p = {"monedas": ["BTC"]}
    s = recorte_en_cada_senal(FAM, m, p, T0, T0 + len(c) * MIN)
    assert len(s) == 1 and s.t[0] == T0 + 3 * DAY + 7 * H   # solo la de las 05:00 (vuelve dentro en la vela de 06:00)
    assert s.stop_pct[0] == pytest.approx((100 - 99 * (1 - R)) + 0.1, rel=1e-9)


def test_barrida_de_un_maximo_viejo_corto(reglas):  # noqa: F811
    c = plano(4)
    c[minuto(1, 20):minuto(1, 21)] = 101.0
    c[minuto(3, 10):minuto(3, 10, 30)] = 101.5
    m = datos.Mercado(velas_mano(c))
    p = {"monedas": ["BTC"]}
    s = recorte_en_cada_senal(FAM, m, p, T0, T0 + len(c) * MIN)
    assert len(s) == 1 and s.dir[0] == -1 and s.t[0] == T0 + 3 * DAY + 11 * H
    sl = (101.5 * (1 + R) - 100) / 100 * 100 + 0.1
    assert s.stop_pct[0] == pytest.approx(sl, rel=1e-9)
    assert s.objetivo_r[0] == pytest.approx(100 * R / max(1.5, sl), rel=1e-9)
    assert not len(FAM.senales(m, {**p, "lados": "largos"}, T0, T0 + len(c) * MIN))


def test_filtro_de_volumen_en_la_vela_de_la_barrida(reglas):  # noqa: F811
    dias = 33
    c = plano(dias)
    c[minuto(30, 20):minuto(30, 21)] = 99.0       # barre el mínimo plano y vuelve en la vela siguiente (sin volumen)
    c[minuto(32, 10):minuto(32, 10, 30)] = 98.5   # barre el de antes con volumen y vuelve en la misma vela
    vol = np.full(len(c), 10.0)
    vol[minuto(32, 10):minuto(32, 10, 30)] = 40.0  # la vela de 1 h: 1.500 frente a 600 de mediana (× 2,5)
    fu = velas_mano(c)
    for k in fu.monedas:
        v = fu.velas[k]
        fu.velas[k] = datos.Velas(v.t, v.o, v.h, v.l, v.c, vol.copy(), vol / 2)
        fu.densos[k] = datos.denso_de(fu.velas[k])
    m = datos.Mercado(fu)
    p = {"monedas": ["BTC"]}
    a, b = T0, T0 + len(c) * MIN
    s = FAM.senales(m, p, a, b)
    assert s.t.tolist() == [T0 + 30 * DAY + 22 * H, T0 + 32 * DAY + 11 * H]
    for x in (1.5, 2.0):
        sv = recorte_en_cada_senal(FAM, m, {**p, "vol_x": x}, a, b)
        assert sv.t.tolist() == [T0 + 32 * DAY + 11 * H]
    assert FAM.filtros_activos(FAM.completar({**p, "vol_x": 1.5})[0]) == ["vol_x"]
