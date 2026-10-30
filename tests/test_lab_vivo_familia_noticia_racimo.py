"""Familia noticia_racimo (H34 y H33 del catálogo): con velas y noticias hechas a mano, la señal sale en el minuto y el
sentido esperados, solo con noticias que ya se podían usar, y pasa la prueba del recorte (un corte en cada señal)."""
import numpy as np
import pytest

from bot.lab_vivo import datos, recorte
from bot.lab_vivo.familias import base, obtener
from bot.lab_vivo.noticias import Noticias
from test_lab_vivo_comun import DAY, H, MIN, T0, reglas, velas_mano  # noqa: F401

TIPOS = datos.TIPOS_SINT
BTC, ETH = 1, 2   # bits de config.COINS


def noticia(t, mon=BTC, dir_=60, n30=4, imp=6, tipo="regulatorio", clase=0):
    return {"t": t, "t0": t - 10 * MIN, "n30": n30, "nmed": n30, "clase": clase, "mon": mon, "dir": dir_, "imp": imp,
            "tipo": TIPOS.index(tipo), "cred": 70, "nov": 60, "mag": 50, "fuente": 1, "medio": 0}


def mercado(c, filas):
    fu = velas_mano(c)
    fu.noticias = Noticias.de_filas(filas, TIPOS, ["a.com"], {"firma": "mano"})
    return datos.Mercado(fu)


def recorte_en_cada_senal(fam, m, p, a, b):
    s = fam.senales(m, p, a, b)
    assert len(s)
    r = recorte.comprobar(fam, [(m, a, b)], p, np.random.default_rng(1), 10, por_senal=len(s))
    assert not recorte.motivos(r) and r["cortes_senal"] == len(np.unique(s.t)), r
    return s


def plano(dias=3):
    n = dias * 1440
    return 100 + 0.1 * np.sin(np.arange(n) / 30.0), n


def test_rango_4h_primera_barra_de_15_min_que_cierra_fuera(reglas):  # noqa: F811
    c, n = plano()
    tn = T0 + DAY + 10 * H                      # se puede usar a las 10:00 del segundo día
    k = (tn - T0) // MIN
    c[k + 20:] = 101.5                          # 20 min después rompe el rango de 4 h (100 ± 0,1)
    m = mercado(c, [noticia(tn),                                  # alcista de BTC en racimo: la señal
                    noticia(tn + 10 * MIN),                       # la misma historia contada otra vez: por flanco, fuera
                    noticia(tn, n30=2),                           # sin racimo de 3
                    noticia(tn, dir_=5),                          # neutral
                    noticia(tn, mon=ETH, dir_=-70)])              # bajista de ETH: no rompe por abajo
    fam = obtener("noticia_racimo")
    p = {}
    s = fam.senales(m, p, T0, T0 + n * MIN)
    assert len(s) == 1 and s.moneda[0] == 0 and s.dir[0] == 1
    assert s.t[0] == tn + 30 * MIN                              # cierre de la barra de 15 min [10:15, 10:30)
    lo = (100 + 0.1 * np.sin(np.arange(k - 240, k) / 30.0)).min() * (1 - 0.0005)
    assert s.stop_pct[0] == pytest.approx(max(1.5, (101.5 - lo) / 101.5 * 100 + 0.1), rel=1e-6)
    assert s.prioridad[0] == 4
    recorte_en_cada_senal(fam, m, p, T0, T0 + n * MIN)
    # a la contra: el mismo minuto, el sentido al revés y el stop de la salida
    sc = fam.senales(m, {"modo": "contra"}, T0, T0 + n * MIN)
    assert len(sc) == 1 and sc.dir[0] == -1 and not np.isfinite(sc.stop_pct[0])
    # n = 5: el racimo de 4 medios no llega
    assert not len(fam.senales(m, {"n": 5}, T0, T0 + n * MIN))
    # con importancia ≥ 7 tampoco (la noticia tiene 6)
    assert not len(fam.senales(m, {"importancia_min": 7}, T0, T0 + n * MIN))


def test_sin_sentido_entra_a_favor_de_la_ruptura(reglas):  # noqa: F811
    c, n = plano()
    tn = T0 + DAY + 10 * H
    k = (tn - T0) // MIN
    c[k + 50:] = 98.0                           # rompe por abajo aunque la noticia sea alcista
    m = mercado(c, [noticia(tn)])
    fam = obtener("noticia_racimo")
    assert not len(fam.senales(m, {}, T0, T0 + n * MIN))
    s = recorte_en_cada_senal(fam, m, {"sentido": "ruptura"}, T0, T0 + n * MIN)
    assert len(s) == 1 and s.dir[0] == -1 and s.t[0] == tn + 60 * MIN


def test_solo_cuentan_las_noticias_que_ya_se_podian_usar(reglas):  # noqa: F811
    c, n = plano()
    tn = T0 + DAY + 10 * H
    k = (tn - T0) // MIN
    c[k - 30:] = 101.5                          # la ruptura es 30 min ANTES de que se pueda usar la noticia …
    m = mercado(c, [noticia(tn)])
    fam = obtener("noticia_racimo")
    assert not len(fam.senales(m, {}, T0, T0 + n * MIN))   # … y el rango de 4 h ya la incluye: no hay señal


def test_reflejo_primer_cierre_que_se_mueve_el_umbral(reglas):  # noqa: F811
    c, n = plano()
    tn = T0 + DAY + 10 * H
    k = (tn - T0) // MIN
    c[k + 12:] = 100.3                          # +0,3 %: no llega al 0,5 %
    c[k + 25:] = 100.7                          # +0,7 % frente al cierre en tn
    m = mercado(c, [noticia(tn)])
    fam = obtener("noticia_racimo")
    p = {"confirmacion": "reflejo"}
    s = recorte_en_cada_senal(fam, m, p, T0, T0 + n * MIN)
    assert len(s) == 1 and s.dir[0] == 1 and s.t[0] == tn + 26 * MIN   # la vela que abre en tn + 25 cierra en + 26
    assert not np.isfinite(s.stop_pct[0])
    sg = base.sigma60_pct(m, "BTC", tn)
    assert sg is not None and 2 * sg < 0.5                    # con este precio tranquilo manda el 0,5 %


def test_fiables_por_dia_con_los_criterios_de_fiables():
    D0 = 20_000
    dia, g, hit = [], [], []
    for d in range(D0, D0 + 10):                # grupo 0: 6 casos al día, 5 aciertos (83 %); grupo 1: la mitad
        for j in range(6):
            dia += [d, d]
            g += [0, 1]
            hit += [int(j < 5), int(j % 2 == 0)]
    tabla = base.fiables_por_dia(np.array(dia), np.array(g), np.array(hit), 2, D0, D0 + 12)
    assert not tabla[3, 0]                      # 4 días: 24 casos (< 30)
    assert base.wilson_bajo(25, 30) >= 0.5 and tabla[4, 0]   # 5 días: 30 casos, 25 aciertos, Wilson 0,69
    assert tabla[9, 0] and not tabla[9, 1]      # 60 casos al 83 %: fiable; al 50 %: no
    # constancia: una semana con ≥ 5 casos por debajo del 50 % lo tumba
    hit2 = [0 if (dd >= D0 + 7 and gg == 0) else h for dd, gg, h in zip(dia, g, hit)]
    tabla2 = base.fiables_por_dia(np.array(dia), np.array(g), np.array(hit2), 2, D0, D0 + 12)
    assert not tabla2[9, 0]
    # ventana de 30 días: lo de hace más de 30 días ya no cuenta
    tabla3 = base.fiables_por_dia(np.array(dia), np.array(g), np.array(hit), 2, D0 + 38, D0 + 42)
    assert not tabla3.any()


def test_grupo_fiable_solo_con_dias_completos_anteriores(reglas):  # noqa: F811
    """Con un grupo que acierta siempre, la variante «fiable» deja pasar las noticias de ese grupo solo cuando ya hay
    30 días UTC completos con sus resultados medidos; la prueba del recorte lo comprueba en cada señal."""
    dias = 50
    n = dias * 1440
    rng = np.random.default_rng(4)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.0004, n)))
    filas = []
    for d in range(1, dias - 1):
        for h_ in (2, 6, 10, 14, 18, 22):        # 6 noticias al día, cada una con su sentido = el de las 4 h siguientes
            t = T0 + d * DAY + h_ * H
            k = (t - T0) // MIN
            dirr = 60 if c[min(n - 1, k + 240)] > c[k - 1] else -60
            filas.append(noticia(t, dir_=dirr))
    fu = velas_mano(c)
    fu.noticias = Noticias.de_filas(filas, TIPOS, ["a.com"], {"firma": "mano"})
    m = datos.Mercado(fu)
    fam = obtener("noticia_racimo")
    p = {"grupo": "fiable", "confirmacion": "reflejo"}
    s_todos = fam.senales(m, {"confirmacion": "reflejo"}, T0, T0 + n * MIN)
    s = fam.senales(m, p, T0, T0 + n * MIN)
    assert len(s) and len(s) < len(s_todos)
    primera = int(s.t.min())
    assert primera >= T0 + 8 * DAY             # hacen falta días medidos (≥ 30 casos en ≥ 3 días y Wilson)
    r = recorte.comprobar(fam, [(m, T0, T0 + n * MIN)], p, np.random.default_rng(2), 10, por_senal=min(20, len(s)))
    assert not recorte.motivos(r), r


def test_sin_noticias_no_hay_senales(reglas):  # noqa: F811
    c, n = plano()
    m = datos.Mercado(velas_mano(c))
    assert m.noticias is None and not len(obtener("noticia_racimo").senales(m, {}, T0, T0 + n * MIN))


def test_medios_por_su_acierto_hasta_la_fecha(reglas):  # noqa: F811
    """El medio 0 acierta siempre, el 1 falla siempre y el 2 casi no publica: «sin_malos» quita al 1 cuando ya tiene
    muestra; «solo_buenos» deja solo al 0 cuando ya la tiene; antes de tener muestra, nadie es malo ni bueno."""
    dias = 26
    n = dias * 1440
    seg = np.arange(n) // 240                                     # tramos de 4 h: sube, baja, sube…
    c = 100 * np.exp(np.cumsum(np.where(seg % 2 == 0, 1e-4, -1e-4)))
    filas = []
    for d in range(1, dias - 1):
        for k in range(6):
            t = T0 + d * DAY + k * 4 * H
            filas.append({**noticia(t), "medio": 0 if k % 2 == 0 else 1})   # alcista: el 0 antes de subir, el 1 de bajar
        if d % 2 == 0:                                            # el 2, un día sí y otro no: < 20 casos en total
            filas.append({**noticia(T0 + d * DAY + 23 * H), "medio": 2})
    fu = velas_mano(c)
    fu.noticias = Noticias.de_filas(filas, TIPOS, ["a.com", "b.com", "c.com"], {"firma": "mano"})
    m = datos.Mercado(fu)
    nt = m.noticias
    sm = base.mascara_medios(m, nt, m.monedas, False, "sin_malos")
    sb = base.mascara_medios(m, nt, m.monedas, False, "solo_buenos")
    dia = (nt.t - T0) // DAY
    assert sm[nt.medio == 0].all() and sm[nt.medio == 2].all()         # el bueno y el que no tiene muestra
    malo = nt.medio == 1
    assert sm[malo & (dia <= 9)].all() and not sm[malo & (dia >= 14)].any()   # el malo, fuera en cuanto tiene muestra
    assert not sb[nt.medio == 2].any() and not sb[malo].any()
    assert not sb[(nt.medio == 0) & (dia <= 9)].any() and sb[(nt.medio == 0) & (dia >= 14)].all()
    assert base.mascara_medios(m, nt, m.monedas, False, "todos").all()
    fam = obtener("noticia_racimo")
    p = {"confirmacion": "reflejo", "medios": "sin_malos"}
    s = fam.senales(m, p, T0, T0 + n * MIN)
    r = recorte.comprobar(fam, [(m, T0, T0 + n * MIN)], p, np.random.default_rng(6), 10, por_senal=min(20, len(s)))
    assert not recorte.motivos(r), r
