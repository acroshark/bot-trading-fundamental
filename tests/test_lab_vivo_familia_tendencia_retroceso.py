"""Familia tendencia_retroceso (Holy Grail): con una tendencia hecha a mano (30 días de subida constante en barras de
4 h), el primer retroceso que toca la EMA(20) y la reanudación, la señal sale al cierre de la barra que reanuda, a favor
y con el stop detrás del retroceso; caduca si la reanudación tarda más de «espera» barras; el espejo bajista da el corto.
Pasa la prueba del recorte con un corte en cada señal. Sin red."""
import numpy as np
import pytest

from bot.lab_vivo import datos, filtros, indicadores as ind, recorte
from bot.lab_vivo.familias import obtener
from test_lab_vivo_comun import MIN, T0, reglas, velas_mano  # noqa: F401

FAM = obtener("tendencia_retroceso")
G = 0.004            # subida por barra de 4 h (log)
NB = 180             # 30 días de tendencia: las barras 0 a 179
DELTA = 0.0425       # el retroceso: −4,25 % en la primera hora de la barra 180 (toca la EMA20, que va ≈ 3,7 % por debajo)
B4 = 240 * MIN


def camino(planas: int = 1, bajista: bool = False) -> np.ndarray:
    """Tendencia, retroceso en la barra 180, «planas» barras quietas abajo, reanudación (vuelve a la mitad del
    retroceso en 30 min) y 6 barras más de tendencia. bajista: el mismo camino invertido (40.000 / precio)."""
    c = 100 * np.exp(G * (np.arange(NB * 240) + 1) / 240)
    P = c[-1]
    ret = np.full(240, P * (1 - DELTA))
    ret[:60] = np.linspace(P, P * (1 - DELTA), 61)[1:]
    quietas = np.full(240 * planas, P * (1 - DELTA))
    rea = np.full(240, P * (1 - DELTA / 2))
    rea[:30] = np.linspace(P * (1 - DELTA), P * (1 - DELTA / 2), 31)[1:]
    sigue = P * (1 - DELTA / 2) * np.exp(G * (np.arange(6 * 240) + 1) / 240)
    cc = np.r_[c, ret, quietas, rea, sigue]
    return 40000.0 / cc if bajista else cc


def barras(m):
    b = m.barras("BTC", 240)
    h, l, cl = (np.asarray(x, float) for x in (b.h, b.l, b.c))
    ax, pdi, mdi = ind.adx(h, l, cl, 14)
    return h, l, cl, ax, pdi, mdi, ind.ema(cl, 20)


def recorte_en_cada_senal(fam, m, p, a, b):
    s = fam.senales(m, p, a, b)
    assert len(s)
    r = recorte.comprobar(fam, [(m, a, b)], p, np.random.default_rng(1), 10, por_senal=len(s))
    assert not recorte.motivos(r) and r["cortes_senal"] == len(np.unique(s.t)), r
    return s


def test_retroceso_a_la_ema_y_reanudacion_alcista(reglas):  # noqa: F811
    cc = camino()
    m = datos.Mercado(velas_mano(cc, monedas=("BTC",)))
    h, l, cl, ax, pdi, mdi, e20 = barras(m)
    s0 = NB                                   # la barra del retroceso: así está hecho el camino
    assert l[s0 - 1] > e20[s0 - 1] and l[s0] <= e20[s0]           # la primera que toca la EMA
    assert ax[s0] >= 30 and pdi[s0] > mdi[s0]                       # con la tendencia fuerte y alcista
    assert cl[s0 + 1] <= h[s0] and cl[s0 + 2] > h[s0 + 1]           # reanuda en la segunda barra
    a, b = T0, T0 + len(cc) * MIN
    s = recorte_en_cada_senal(FAM, m, {}, a, b)
    assert len(s) == 1 and s.dir[0] == 1 and s.moneda[0] == 0
    assert s.t[0] == T0 + (s0 + 3) * B4                             # el cierre de la barra s0 + 2
    sl = (cl[s0 + 2] - l[s0:s0 + 3].min()) / cl[s0 + 2] * 100 + 0.1
    assert s.stop_pct[0] == pytest.approx(sl, rel=1e-9) and 1.5 < sl < 5
    assert s.prioridad[0] == pytest.approx(ax[s0])
    assert not len(FAM.senales(m, {"lados": "cortos"}, a, b))
    assert not set(FAM.parametros) & set(filtros.PARAMS)            # ningún parámetro tapa un filtro común


def test_caduca_si_la_reanudacion_tarda(reglas):  # noqa: F811
    cc = camino(planas=4)                     # 4 barras quietas: reanuda en la barra s0 + 5
    m = datos.Mercado(velas_mano(cc, monedas=("BTC",)))
    a, b = T0, T0 + len(cc) * MIN
    assert not len(FAM.senales(m, {"espera": 3}, a, b))
    s = recorte_en_cada_senal(FAM, m, {"espera": 6}, a, b)
    assert len(s) == 1 and s.dir[0] == 1 and s.t[0] == T0 + (NB + 6) * B4


def test_espejo_bajista_da_el_corto(reglas):  # noqa: F811
    cc = camino(bajista=True)
    m = datos.Mercado(velas_mano(cc, monedas=("BTC",)))
    h, l, cl, ax, pdi, mdi, e20 = barras(m)
    s0 = NB
    assert h[s0 - 1] < e20[s0 - 1] and h[s0] >= e20[s0] and ax[s0] >= 30 and mdi[s0] > pdi[s0]
    a, b = T0, T0 + len(cc) * MIN
    s = recorte_en_cada_senal(FAM, m, {}, a, b)
    assert len(s) == 1 and s.dir[0] == -1 and s.t[0] == T0 + (s0 + 3) * B4
    assert s.stop_pct[0] == pytest.approx((h[s0:s0 + 3].max() - cl[s0 + 2]) / cl[s0 + 2] * 100 + 0.1, rel=1e-9)


def test_sin_el_di_a_favor_no_es_un_retroceso(reglas):  # noqa: F811
    """En barras de 1 h, la misma caída cabe en una sola barra: toca la EMA20 con el ADX alto, pero ya con −DI > +DI.
    Eso no es un retroceso dentro de una tendencia alcista, sino un giro: no hay señal (ni larga ni corta)."""
    cc = camino()
    m = datos.Mercado(velas_mano(cc, monedas=("BTC",)))
    b = m.barras("BTC", 60)
    h, l, cl = (np.asarray(x, float) for x in (b.h, b.l, b.c))
    ax, pdi, mdi = ind.adx(h, l, cl, 14)
    e20 = ind.ema(cl, 20)
    i = NB * 4                                # la vela de 1 h de la caída
    assert l[i - 1] > e20[i - 1] and l[i] <= e20[i] and ax[i] >= 30 and mdi[i] > pdi[i]
    assert not len(FAM.senales(m, {"tf_min": 60}, T0, T0 + len(cc) * MIN))
