"""Familia impulso_noticias y filtro común noticias_previas: las entradas son las de apertura_impulso tal cual y el
filtro solo quita las que van contra las noticias de las horas previas (o deja solo las que van a favor), con las
noticias que ya se podían usar antes de la decisión; pasa la prueba del recorte en cada señal."""
import numpy as np

from bot.lab_vivo import datos, filtros, recorte
from bot.lab_vivo.familias import base, impulso_noticias, obtener
from bot.lab_vivo.familias.base import Senales
from bot.lab_vivo.noticias import Noticias
from test_lab_vivo_comun import DAY, H, MIN, T0, fuente, reglas  # noqa: F401

TIPOS = datos.TIPOS_SINT


def _n(filas):
    return Noticias.de_filas([{"t0": f["t"], "n30": 3, "nmed": 3, "imp": 5, **f} for f in filas], TIPOS, ["a.com"],
                             {"firma": "mano"})


class _M:
    monedas = ("BTC", "ETH", "SOL", "XRP")

    def __init__(self, n):
        self.noticias = n


def test_suma_noticias_solo_las_previas_de_esa_moneda():
    t = T0 + 10 * H
    n = _n([{"t": t - 9 * H, "mon": 1, "dir": 60},            # más de 8 h antes: fuera
            {"t": t - 7 * H, "mon": 1, "dir": 60},            # BTC alcista
            {"t": t - 6 * H, "mon": 1, "dir": 60, "imp": 8},  # BTC alcista e importante
            {"t": t - 5 * H, "mon": 2, "dir": -60},           # ETH bajista
            {"t": t - 4 * H, "mon": 0, "dir": -50},           # general bajista: para las cuatro
            {"t": t - 3 * H, "mon": 1, "dir": 10},            # neutral: no cuenta
            {"t": t, "mon": 1, "dir": -90}])                  # en el mismo minuto de la decisión: aún no
    s = Senales(t=np.array([t, t, t], np.int64), moneda=np.array([0, 1, 2], np.int8), dir=np.array([1, 1, -1], np.int8))
    m = _M(n)
    assert list(filtros.suma_noticias(m, s, 8)) == [1, -2, -1]
    assert list(filtros.suma_noticias(m, s, 8, generales=False)) == [2, -1, 0]
    assert list(filtros.suma_noticias(m, s, 8, imp_min=7)) == [1, 0, 0]
    assert list(filtros.suma_noticias(m, s, 4)) == [-1, -1, -1]
    assert list(filtros.suma_noticias(_M(None), s, 8)) == [0, 0, 0]
    # no_contra: BTC largo con +1 pasa, ETH largo con −2 no, SOL corto con −1 pasa; a_favor: BTC y SOL
    p = {"noticias_previas": "no_contra", "noticias_horas": 8, "noticias_imp_min": 0, "noticias_generales": "todas"}
    assert list(filtros.aplicar(m, s, p).moneda) == [0, 2]
    assert list(filtros.aplicar(m, s, {**p, "noticias_previas": "a_favor"}).moneda) == [0, 2]
    assert list(filtros.aplicar(m, s, {**p, "noticias_previas": "a_favor", "noticias_generales": "no"}).moneda) == [0]
    assert len(filtros.aplicar(m, s, {**p, "noticias_previas": "todas"})) == 3
    # mayoría: BTC 2 alcistas de 4 (con la neutral y la general bajista) no llega; ETH corto… no: ETH va largo con 0 de 2
    assert list(filtros.suma_noticias(m, s, 8, cuenta="todas")) == [4, 2, 1]
    assert list(filtros.aplicar(m, s, {**p, "noticias_previas": "mayoria"}).moneda) == [2]   # SOL: 1 bajista de 1


def test_las_entradas_son_las_de_apertura_impulso(reglas):  # noqa: F811
    m = datos.Mercado(fuente(dias=90, semilla=22, noticias=True))
    a, b = m.periodo["inicio"], m.periodo["fin"]
    fam, ref = obtener("impulso_noticias"), obtener("apertura_impulso")
    for p in ({}, {"measure_min": 30, "min_move_pct": 0.2, "entry": "t:60", "rvol_min": 2.0}):
        s0 = ref.senales(m, p, a, b)
        assert len(s0) and fam.senales(m, p, a, b).iguales(s0)            # sin el filtro, idénticas
        for modo in ("no_contra", "a_favor"):
            s = fam.senales(m, {**p, "noticias_previas": modo}, a, b)
            clave0 = set(zip(s0.t.tolist(), s0.moneda.tolist(), s0.dir.tolist()))
            assert 0 < len(s) < len(s0) and set(zip(s.t.tolist(), s.moneda.tolist(), s.dir.tolist())) <= clave0
            sg = np.sign(filtros.suma_noticias(m, s, 8))
            assert np.all(sg != -s.dir) if modo == "no_contra" else np.all(sg == s.dir)


def test_recorte_en_cada_senal(reglas):  # noqa: F811
    m = datos.Mercado(fuente(dias=90, semilla=22, noticias=True))
    fam = obtener("impulso_noticias")
    a = m.periodo["inicio"] + 40 * DAY
    p = {"noticias_previas": "a_favor", "noticias_horas": 4}
    s = fam.senales(m, p, a, a + 30 * DAY)
    r = recorte.comprobar(fam, [(m, a, a + 30 * DAY)], p, np.random.default_rng(5), 10, por_senal=min(40, len(s)))
    assert len(s) and not recorte.motivos(r) and r["cortes_senal"] > 0, r


def test_la_base_esta_fijada():
    assert impulso_noticias._sha_base() == impulso_noticias.BASE_SHA
    assert obtener("apertura_impulso").version == impulso_noticias.BASE_VERSION
    fam = obtener("impulso_noticias")
    assert "noticias_3a" in fam.datos and fam.momento == "aperturas"
    assert fam.filtros_activos(fam.completar({"noticias_previas": "no_contra", "noticias_imp_min": 7,
                                              "noticias_horas": 4})[0]) == ["noticias_previas", "noticias_imp_min"]
    assert base.FIABLE["acierto_min"] == 0.60
