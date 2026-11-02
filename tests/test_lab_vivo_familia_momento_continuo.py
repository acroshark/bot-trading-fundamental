"""Familia momento_continuo (Frog in the Pan): con cierres diarios hechos a mano, una subida hecha de días pequeños y
seguidos da la señal larga el primer día en que la continuidad llega al mínimo (calculado a mano abajo), y una subida
igual de fuerte hecha a saltos no da ninguna. Pasa la prueba del recorte con un corte en cada señal. Sin red."""
import numpy as np
import pytest

from bot.lab_vivo import datos, filtros, recorte
from bot.lab_vivo.familias import obtener
from bot.lab_vivo.familias.momento_continuo import discrecion
from test_lab_vivo_comun import DAY, T0, reglas, velas_mano  # noqa: F401

FAM = obtener("momento_continuo")
EPS = 0.001          # días 1 a 99: +0,1 % los impares y −0,1 % los pares (nada de tendencia)
DELTA = 0.01         # días 100 a 127 (subida continua): +1 % cada día


def cierres(fase) -> np.ndarray:
    r = [0.0] + [EPS if i % 2 else -EPS for i in range(1, 100)] + list(fase)
    return 100 * np.exp(np.cumsum(r))


def mercado(D):
    return datos.Mercado(velas_mano(np.repeat(D, 1440)))     # cada día, todos sus minutos en su cierre


def recorte_en_cada_senal(fam, m, p, a, b):
    s = fam.senales(m, p, a, b)
    assert len(s)
    r = recorte.comprobar(fam, [(m, a, b)], p, np.random.default_rng(1), 10, por_senal=len(s))
    assert not recorte.motivos(r) and r["cortes_senal"] == len(np.unique(s.t)), r
    return s


def test_discrecion_a_mano():
    c = np.exp(np.cumsum([0.0, 0.01, 0.01, -0.005, 0.01, 0.0]))   # rendimientos: +, +, −, +, 0
    pret, idd = discrecion(c, 4)
    assert np.isnan(pret[:4]).all() and np.isnan(idd[:4]).all()
    assert pret[4] == pytest.approx(0.025) and idd[4] == pytest.approx((1 - 3) / 4)     # 3 a favor y 1 en contra
    assert pret[5] == pytest.approx(0.015) and idd[5] == pytest.approx((1 - 2) / 4)     # el 0 no cuenta
    assert not set(FAM.parametros) & set(filtros.PARAMS)   # ningún parámetro tapa un filtro común


def test_subida_continua_entra_el_dia_esperado(reglas):  # noqa: F811
    """Con k días de subida (días 100 a 99 + k), la ventana de 28 tiene 28 − k días alternos: si k es par se anulan; si
    es impar sobra un +0,1 % (empiezan y acaban en impar). Así, pos − neg = k (par) o k + 1 (impar), e ID = −(pos −
    neg) / 28 ≤ −0,2 pide pos − neg ≥ 5,6: el primer día es k = 5 (pos − neg = 6), el día 104. z ya es grande."""
    D = cierres([DELTA] * 28)
    m = mercado(D)
    a, b = T0, T0 + len(D) * DAY
    s = recorte_en_cada_senal(FAM, m, {"monedas": ["BTC"]}, a, b)
    dias = ((s.t - T0) // DAY - 1).tolist()            # el día cuyo cierre decide (la decisión es a las 00:00 UTC)
    assert dias == list(range(104, 127)) and np.all(s.dir == 1)
    assert s.prioridad[0] == pytest.approx(6 / 28)
    assert np.all(np.isfinite(s.stop_pct)) and np.all(s.stop_pct > 0)
    assert np.all(s.t % DAY == 0)
    # por flanco: solo el primer día; con más continuidad (0,3): pos − neg ≥ 8,4, que no da k = 8 (par: 8) y sí k = 9
    # (impar: 10), el día 108
    sf = recorte_en_cada_senal(FAM, m, {"monedas": ["BTC"], "modo": "flanco"}, a, b)
    assert ((sf.t - T0) // DAY - 1).tolist() == [104]
    s3 = FAM.senales(m, {"monedas": ["BTC"], "id_min": 0.3}, a, b)
    assert int((s3.t[0] - T0) // DAY - 1) == 108
    assert not len(FAM.senales(m, {"monedas": ["BTC"], "lados": "cortos"}, a, b))


def test_la_misma_subida_a_saltos_no_entra(reglas):  # noqa: F811
    fase = [-0.002] * 28
    fase[2] = fase[12] = 0.12                           # dos saltos de +12 % y el resto, −0,2 % cada día
    D = cierres(fase)
    m = mercado(D)
    a, b = T0, T0 + len(D) * DAY
    pret, idd = discrecion(np.asarray(m.barras("BTC", 1440).c, float), 28)
    assert pret[127] > 0.18 and idd[127] > 0.5           # sube mucho, pero a saltos (casi todos los días en contra)
    for p in ({}, {"modo": "flanco"}, {"id_min": 0.1}):
        assert not len(FAM.senales(m, {"monedas": ["BTC"], **p}, a, b))
