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


def _atr_diario_pct(D, i: int, n: int = 14) -> float:
    """ATR(14) de Wilder en % del cierre del día i, a mano (para el stop), con los cierres diarios D de velas_mano: cada
    minuto abre en el cierre anterior y su máximo y mínimo van a ± 0,05 % del cuerpo."""
    R = 0.0005
    tr = []
    for j in range(i + 1):
        o = D[j - 1] if j else D[0]
        h, l = max(o, D[j]) * (1 + R), min(o, D[j]) * (1 - R)
        pc = D[j - 1] if j else D[0]
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(tr[:n]) / n
    for j in range(n, i + 1):
        a += (tr[j] - a) / n
    return a / D[i] * 100


def test_el_stop_es_k_atr_por_el_atr_diario(reglas):  # noqa: F811
    D = cierres([DELTA] * 28)
    m = mercado(D)
    a, b = T0, T0 + len(D) * DAY
    for k in (0.75, 1.0):
        s = FAM.senales(m, {"monedas": ["BTC"], "k_atr": k}, a, b)
        for t, sl in zip(s.t[:3].tolist(), s.stop_pct[:3].tolist()):
            dia = int((t - T0) // DAY) - 1                    # el último día cerrado en la decisión
            assert sl == pytest.approx(k * _atr_diario_pct(D, dia), rel=1e-9)


def _oraculo(D, L=28, n=90, z_min=0.5, id_min=0.2) -> list[int]:
    """Los días que deciden, con la definición escrita con bucles (sin la familia): z = PRET / (σ × √L), con σ la
    desviación (n − 1) de los n rendimientos diarios anteriores al día, e ID de los L rendimientos que acaban en él.
    Sin el último día: su decisión cae justo al final de los datos."""
    lg = [float(np.log(x)) for x in D]
    r = [0.0] + [lg[j] - lg[j - 1] for j in range(1, len(D))]
    dias = []
    for i in range(max(L, n) + 1, len(D) - 1):
        pret = lg[i] - lg[i - L]
        prev = r[i - n:i]
        mu = sum(prev) / n
        sd = (sum((x - mu) ** 2 for x in prev) / (n - 1)) ** 0.5
        z = pret / (sd * L ** 0.5)
        ven = r[i - L + 1:i + 1]
        idd = (1 if pret > 0 else -1) * (sum(x < 0 for x in ven) - sum(x > 0 for x in ven)) / L
        if abs(z) >= z_min and idd <= -id_min + 1e-12:
            dias.append(i)
    return dias


def test_el_z_decide_cuando_el_camino_previo_es_muy_volatil(reglas):  # noqa: F811
    """Antes, ±2 % cada día (σ ≈ 2 %); luego +0,5 % cada día: la continuidad llega el día 104, como arriba, pero el z
    (con σ de 90 días y √28) no llega a 0,5 hasta el 106 ni a 1 hasta el 116. La familia da los mismos días que la
    definición escrita a mano."""
    r = [0.0] + [0.02 if i % 2 else -0.02 for i in range(1, 100)] + [0.005] * 28
    D = 100 * np.exp(np.cumsum(r))
    m = mercado(D)
    a, b = T0, T0 + len(D) * DAY
    assert _oraculo(D, z_min=0.0)[0] == 104
    for z_min, primero in ((0.5, 106), (1.0, 116)):
        s = FAM.senales(m, {"monedas": ["BTC"], "z_min": z_min}, a, b)
        dias = ((s.t - T0) // DAY - 1).tolist()
        assert dias == _oraculo(D, z_min=z_min) and dias[0] == primero


def test_un_dia_sin_datos_no_parte_la_racha(reglas):  # noqa: F811
    D = cierres([DELTA] * 28)
    fu = velas_mano(np.repeat(D, 1440))
    quita = np.ones(len(D) * 1440, bool)
    quita[110 * 1440 + 600:110 * 1440 + 780] = False        # 3 h sin velas el día 110: no llega al 90 %
    for k in fu.monedas:
        v = fu.velas[k]
        fu.velas[k] = datos.Velas(*(getattr(v, x)[quita] for x in ("t", "o", "h", "l", "c", "v", "tb")))
        fu.densos[k] = datos.denso_de(fu.velas[k])
    m = datos.Mercado(fu)
    a, b = T0, T0 + len(D) * DAY
    sf = recorte_en_cada_senal(FAM, m, {"monedas": ["BTC"], "modo": "flanco"}, a, b)
    assert ((sf.t - T0) // DAY - 1).tolist() == [104]          # no vuelve a entrar el día 111
    se = FAM.senales(m, {"monedas": ["BTC"]}, a, b)
    assert ((se.t - T0) // DAY - 1).tolist() == [d for d in range(104, 127) if d != 110]   # el 110 no decide
