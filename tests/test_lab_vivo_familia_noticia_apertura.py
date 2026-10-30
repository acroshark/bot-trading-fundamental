"""Familia noticia_apertura: con velas, aperturas y noticias hechas a mano, la señal sale a la apertura + los minutos
de impulso, en el sentido de las noticias de las horas previas, solo si el impulso lo confirma, y pasa la prueba del
recorte (un corte en cada señal)."""
import numpy as np

from bot.lab_vivo import datos, recorte
from bot.lab_vivo.familias import obtener
from bot.lab_vivo.noticias import Noticias
from test_lab_vivo_comun import DAY, H, MIN, T0, reglas, velas_mano  # noqa: F401
from test_lab_vivo_familia_noticia_racimo import BTC, ETH, TIPOS, noticia

AP1 = T0 + DAY + 8 * H            # «europa» el martes a las 08:00 UTC
AP2 = T0 + 2 * DAY + 8 * H
AP3 = T0 + 3 * DAY + 8 * H


def apertura(t, w="europa"):
    return {"id": w, "name": w, "markets": [], "lead": "", "open_ms": t, "day": "", "holiday": []}


def mercado(c, filas, aps):
    fu = velas_mano(c)
    fu.noticias = Noticias.de_filas(filas, TIPOS, ["a.com"], {"firma": "mano"})
    fu.aperturas = aps
    return datos.Mercado(fu)


def precios():
    n = 5 * 1440
    c = np.full(n, 100.0)
    for ap, mov in ((AP1, 0.6), (AP2, 0.6), (AP3, -0.6)):
        k = (ap - T0) // MIN
        c[k:] = 100.0 * (1 + mov / 100)          # el impulso de los primeros minutos: +0,6, +0,6 y −0,6 %
    return c, n


def test_noticias_previas_confirmadas_por_el_impulso(reglas):  # noqa: F811
    c, n = precios()
    filas = [noticia(AP1 - 3 * H), noticia(AP1 - 2 * H, mon=ETH, dir_=-50),   # BTC alcista; ETH bajista
             noticia(AP2 - 2 * H, dir_=-60),                                   # bajista y el precio sube: nada
             noticia(AP3 + 5 * MIN, dir_=-60),                                 # después de la apertura: no cuenta
             noticia(AP3 - 9 * H, dir_=-60)]                                   # más de 8 h antes: no cuenta
    m = mercado(c, filas, [apertura(AP1), apertura(AP2), apertura(AP3), apertura(AP3, "nuevayork")])
    fam = obtener("noticia_apertura")
    p = {"generales": "no", "windows": ["europa"]}
    s = fam.senales(m, p, T0, T0 + n * MIN)
    assert len(s) == 1 and s.moneda[0] == 0 and s.dir[0] == 1 and s.t[0] == AP1 + 15 * MIN
    assert s.ventana[0] == 2                      # «europa» en apertura.LAB_WINDOWS
    r = recorte.comprobar(fam, [(m, T0, T0 + n * MIN)], p, np.random.default_rng(3), 10, por_senal=len(s))
    assert not recorte.motivos(r) and r["cortes_senal"] == 1
    # a la contra, la misma decisión al revés
    sc = fam.senales(m, {**p, "modo": "contra"}, T0, T0 + n * MIN)
    assert len(sc) == 1 and sc.dir[0] == -1 and sc.t[0] == AP1 + 15 * MIN
    # el 0,6 % de impulso pasa el 0,5 % mínimo; con 60 min de medida decide una hora después
    assert len(fam.senales(m, {**p, "min_mov_pct": 0.5}, T0, T0 + n * MIN)) == 1
    s60 = fam.senales(m, {**p, "medida_min": 60}, T0, T0 + n * MIN)
    assert len(s60) == 1 and s60.t[0] == AP1 + 60 * MIN
    assert not len(fam.senales(m, {**p, "n": 5}, T0, T0 + n * MIN))   # el racimo de 4 medios no llega a 5


def test_un_impulso_pequeno_no_confirma(reglas):  # noqa: F811
    n = 3 * 1440
    c = np.full(n, 100.0)
    c[(AP1 - T0) // MIN:] = 100.1                  # +0,1 % < 0,2 %
    m = mercado(c, [noticia(AP1 - 3 * H)], [apertura(AP1)])
    fam = obtener("noticia_apertura")
    assert not len(fam.senales(m, {"windows": ["europa"]}, T0, T0 + n * MIN))
    assert len(fam.senales(m, {"windows": ["europa"], "min_mov_pct": 0.0}, T0, T0 + n * MIN)) == 1


def test_las_generales_valen_para_las_cuatro_y_se_suman(reglas):  # noqa: F811
    c, n = precios()
    filas = [noticia(AP1 - 3 * H, mon=0, clase=1, tipo="macro"),               # macro alcista: vale para todas
             noticia(AP1 - 2 * H, mon=ETH, dir_=-50),                          # ETH: +1 − 1 = 0, nada
             noticia(AP1 - 1 * H, mon=0, dir_=-40)]                            # general bajista: BTC 1 − 1 = 0 …
    m = mercado(c, filas[:2], [apertura(AP1)])
    fam = obtener("noticia_apertura")
    s = fam.senales(m, {"windows": ["europa"]}, T0, T0 + n * MIN)
    assert sorted(s.moneda.tolist()) == [0, 2, 3] and set(s.dir.tolist()) == {1}
    m2 = mercado(c, filas, [apertura(AP1)])
    assert not len(fam.senales(m2, {"windows": ["europa"]}, T0, T0 + n * MIN))   # … y todas se anulan


def test_sin_noticias_no_hay_senales(reglas):  # noqa: F811
    c, n = precios()
    fu = velas_mano(c)
    fu.aperturas = [apertura(AP1)]
    assert not len(obtener("noticia_apertura").senales(datos.Mercado(fu), {}, T0, T0 + n * MIN))
