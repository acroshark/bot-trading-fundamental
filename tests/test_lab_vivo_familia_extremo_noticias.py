"""Familia extremo_noticias (Chan, 2003): con velas y noticias hechas a mano, un movimiento extremo de 1 h sin noticias
de esa moneda da la señal a la contra; con una noticia suya antes (que ya se podía usar), no, y con «con» y «favor»
da la continuación; «da_igual» da las dos. Solo cuentan las noticias anteriores a la decisión, las generales cuentan
para las cuatro monedas y, sin ninguna historia en 24 h (un hueco del histórico), no se decide. Pasa la prueba del
recorte con un corte en cada señal. Sin red."""
import numpy as np
import pytest

from bot.lab_vivo import datos, filtros, indicadores as ind, recorte
from bot.lab_vivo.familias import base, obtener
from bot.lab_vivo.noticias import Noticias
from test_lab_vivo_comun import DAY, H, MIN, T0, reglas, velas_mano  # noqa: F401

TIPOS = datos.TIPOS_SINT
BTC, ETH = 1, 2      # bits de config.COINS
R = 0.0005           # el rango de velas_mano
FAM = obtener("extremo_noticias")
N = 32 * 1440
K = 31 * 1440 + 10 * 60 + 20      # el día 31 a las 10:20 BTC (y las demás) cae un 3 % y se queda ahí
TD = T0 + 31 * DAY + 11 * H       # la decisión: el cierre de la vela de 1 h de las 10:00


def noticia(t, mon=BTC, n30=2, dir_=40, clase=0):
    return {"t": t, "t0": t - 10 * MIN, "n30": n30, "nmed": n30, "clase": clase, "mon": mon, "dir": dir_, "imp": 6,
            "tipo": TIPOS.index("mercado"), "cred": 70, "nov": 60, "mag": 50, "fuente": 1, "medio": 0}


def precios() -> np.ndarray:
    c = 100 + 0.1 * np.sin(np.arange(N) / 30.0)   # tranquilo (σ de 1 h ≈ 0,12 %) …
    c[K:] = 97.0                                   # … y una caída de 25 σ
    return c


def mercado(filas):
    fu = velas_mano(precios())
    fu.noticias = Noticias.de_filas(filas, TIPOS, ["a.com"], {"firma": "mano"})
    return datos.Mercado(fu)


def recorte_en_cada_senal(fam, m, p, a, b):
    s = fam.senales(m, p, a, b)
    assert len(s)
    r = recorte.comprobar(fam, [(m, a, b)], p, np.random.default_rng(1), 10, por_senal=len(s))
    assert not recorte.motivos(r) and r["cortes_senal"] == len(np.unique(s.t)), r
    return s


def senales(m, **p):
    return FAM.senales(m, {"monedas": ["BTC"], **p}, T0, T0 + N * MIN)


def z_a_mano(m, w: int, i: int = 31 * 24 + 10) -> float:
    """z de la vela i de BTC con numpy (sin la familia): ln(C_i / C_{i−w}) / (σ × √w), con σ la desviación (n − 1) de
    los 720 rendimientos de 1 h anteriores a i."""
    cl = np.asarray(m.barras("BTC", 60).c, float)
    r = np.diff(np.log(cl))                  # r[j] va de la vela j a la j + 1
    sd = np.std(r[i - 721:i - 1], ddof=1)
    return float(np.log(cl[i] / cl[i - w]) / (sd * np.sqrt(w)))


def sin_velas(fu, desde: int, minutos: int):
    """Quita las velas de [desde, desde + minutos) de las cuatro monedas (un hueco de datos)."""
    quita = np.ones(N, bool)
    quita[desde:desde + minutos] = False
    for k in fu.monedas:
        v = fu.velas[k]
        fu.velas[k] = datos.Velas(*(getattr(v, x)[quita] for x in ("t", "o", "h", "l", "c", "v", "tb")))
        fu.densos[k] = datos.denso_de(fu.velas[k])
    return fu


def test_sin_noticias_a_la_contra(reglas):  # noqa: F811
    m = mercado([noticia(TD - 2 * H, mon=ETH)])      # solo una de ETH, 2 h antes (hay datos, pero nada de BTC)
    s = recorte_en_cada_senal(FAM, m, {"monedas": ["BTC"]}, T0, T0 + N * MIN)
    assert len(s) == 1 and s.t[0] == TD and s.dir[0] == 1 and s.moneda[0] == 0
    c = precios()
    previo = c[K - 20 - 1]                            # el cierre de la vela de las 09:00 (el minuto 09:59)
    sl = R * 100 + 0.2                                # cierra en 97 y su mínimo es 97 × (1 − R)
    assert s.stop_pct[0] == pytest.approx(sl, rel=1e-9)
    assert s.objetivo_r[0] == pytest.approx((previo - 97) / 97 * 100 / 1.5, rel=1e-9)   # deshacer la caída, en R
    assert s.prioridad[0] == pytest.approx(abs(z_a_mano(m, 1)), rel=1e-9) and s.prioridad[0] > 20
    # la de ETH es de ETH: para ETH sí hay noticia y no da señal; para SOL y XRP, sí
    s4 = FAM.senales(m, {}, T0, T0 + N * MIN)
    assert sorted(s4.moneda.tolist()) == [0, 2, 3]
    # «con» y «favor»: nada (no hay noticia de BTC); «da_igual»: la misma
    assert not len(senales(m, noticias="con", modo="favor"))
    assert senales(m, noticias="da_igual").iguales(s)
    # el movimiento de 4 h también es extremo y se decide en el mismo minuto; el objetivo, desde hace 4 velas
    s4h = senales(m, horas_mov=4)
    assert s4h.t.tolist() == [TD] and s4h.objetivo_r[0] == pytest.approx((c[K - 20 - 3 * 60 - 1] - 97) / 97 * 100 / 1.5)
    assert not set(FAM.parametros) & set(filtros.PARAMS)   # ningún parámetro tapa un filtro común


def test_con_noticia_de_la_moneda_la_continuacion(reglas):  # noqa: F811
    m = mercado([noticia(T0 + 31 * DAY, mon=ETH), noticia(TD - 3 * H, mon=BTC)])
    assert not len(senales(m))                         # había noticia de BTC 3 h antes: no es liquidez
    s = recorte_en_cada_senal(FAM, m, {"monedas": ["BTC"], "noticias": "con", "modo": "favor"}, T0, T0 + N * MIN)
    assert len(s) == 1 and s.t[0] == TD and s.dir[0] == -1 and not np.isfinite(s.objetivo_r[0])
    atr4 = base.atr_pct_en(m, "BTC", 240, np.array([TD], np.int64))[0]
    assert s.stop_pct[0] == pytest.approx(1.5 * atr4, rel=1e-9)
    assert senales(m, noticias="da_igual").t.tolist() == [TD]
    # la ventana: con 6 h la noticia de hace 3 h cuenta; si fuera de hace 8 h, solo con 12 h
    m8 = mercado([noticia(TD - 8 * H, mon=BTC)])
    assert senales(m8).t.tolist() == [TD] and not len(senales(m8, ventana_h=12))


def test_que_cuenta_como_noticia(reglas):  # noqa: F811
    # una general (no nombra moneda) cuenta para las cuatro … salvo con generales «no»
    mg = mercado([noticia(TD - 2 * H, mon=0, clase=1)])
    assert not len(senales(mg)) and senales(mg, generales="no").t.tolist() == [TD]
    # un racimo de 2 medios no llega a n = 3
    m2 = mercado([noticia(TD - 2 * H, mon=BTC, n30=2)])
    assert not len(senales(m2)) and senales(m2, n=3).t.tolist() == [TD]
    # el sentido de la noticia no importa: una bajista (como la caída) también quita la señal
    assert not len(senales(mercado([noticia(TD - 2 * H, mon=BTC, dir_=-80)])))
    # una que se puede usar justo en el minuto de la decisión aún no se conocía: no cuenta
    mt = mercado([noticia(TD - 20 * H, mon=ETH), noticia(TD, mon=BTC)])
    assert senales(mt).t.tolist() == [TD]
    recorte_en_cada_senal(FAM, mt, {"monedas": ["BTC"]}, T0, T0 + N * MIN)


def test_sin_historias_en_24_h_no_se_decide(reglas):  # noqa: F811
    m = mercado([noticia(TD - 30 * H, mon=ETH)])      # la última historia, de hace 30 h: un hueco del histórico
    for p in ({}, {"noticias": "con", "modo": "favor"}, {"noticias": "da_igual"}):
        assert not len(senales(m, **p))
    fu = velas_mano(precios())                         # y sin el archivo de noticias, nada
    assert not len(FAM.senales(datos.Mercado(fu), {}, T0, T0 + N * MIN))


def test_a_favor_el_stop_es_1_5_atr_de_4_h(reglas):  # noqa: F811
    m = mercado([noticia(TD - 3 * H, mon=BTC)])
    s = senales(m, noticias="con", modo="favor")
    b4 = m.barras("BTC", 240)
    k = int(np.searchsorted(b4.t_cierre, TD, "right")) - 1          # la última barra de 4 h cerrada en la decisión
    atr4 = ind.atr_pct(b4.h, b4.l, b4.c, 14)[k]
    b1 = m.barras("BTC", 60)
    atr1 = ind.atr_pct(b1.h, b1.l, b1.c, 14)[int(np.searchsorted(b1.t_cierre, TD, "right")) - 1]
    assert s.stop_pct[0] == pytest.approx(1.5 * atr4, rel=1e-9) and abs(atr4 - atr1) > 0.01


def test_un_hueco_de_datos_no_es_un_movimiento(reglas):  # noqa: F811
    """Sin velas de 08:00 a 09:59 y la caída dentro del hueco: la vela de 10:00 es válida y plana en 97. Su base (el
    cierre de las 09:00) es el último antes del hueco: no hay movimiento de 1 h que medir."""
    c = precios()
    a = 31 * 1440 + 8 * 60
    c[a:] = 97.0
    fu = sin_velas(velas_mano(c), a, 120)
    fu.noticias = Noticias.de_filas([noticia(TD - 2 * H, mon=ETH)], TIPOS, ["a.com"], {"firma": "mano"})
    assert not len(senales(datos.Mercado(fu)))


def test_las_velas_del_movimiento_de_4_h_tienen_que_ser_validas(reglas):  # noqa: F811
    """Un hueco a las 08:00 y la caída a las 10:20: con 1 h (velas de las 09:00 y las 10:00, válidas) se decide a las
    11:00; con 4 h, no hasta que la ventana de 4 velas no tiene el hueco (de 09:00 a 13:59: a las 14:00)."""
    fu = sin_velas(velas_mano(precios()), 31 * 1440 + 8 * 60, 60)
    fu.noticias = Noticias.de_filas([noticia(TD - 2 * H, mon=ETH)], TIPOS, ["a.com"], {"firma": "mano"})
    m = datos.Mercado(fu)
    assert senales(m).t.tolist() == [TD]
    s4 = recorte_en_cada_senal(FAM, m, {"monedas": ["BTC"], "horas_mov": 4}, T0, T0 + N * MIN)
    assert s4.t.tolist() == [TD + 3 * H]


def test_el_mismo_movimiento_no_decide_dos_veces(reglas):  # noqa: F811
    """Cae a las 10:20, vuelve a las 11:20 y cae otra vez a las 12:20. Con 4 h, el flanco de las 13:00 (a 2 velas del
    primero) y el de las 16:00 (a 3 del anterior) son el mismo movimiento: solo decide el de las 11:00. Con 1 h, cada
    vela es un movimiento: decide las tres veces (largo, corto y largo)."""
    c = precios()
    c[K:K + 60] = 97.0
    c[K + 60:K + 120] = 100.0
    c[K + 120:] = 97.0
    fu = velas_mano(c)
    fu.noticias = Noticias.de_filas([noticia(TD - 2 * H, mon=ETH)], TIPOS, ["a.com"], {"firma": "mano"})
    m = datos.Mercado(fu)
    i = 31 * 24 + 10
    assert abs(z_a_mano(m, 4, i)) >= 3 > abs(z_a_mano(m, 4, i + 1)) and abs(z_a_mano(m, 4, i + 2)) >= 3   # 3 flancos
    s4 = recorte_en_cada_senal(FAM, m, {"monedas": ["BTC"], "horas_mov": 4}, T0, T0 + N * MIN)
    assert s4.t.tolist() == [TD]
    s1 = senales(m)
    assert s1.t.tolist() == [TD, TD + H, TD + 2 * H] and s1.dir.tolist() == [1, -1, 1]


def test_el_movimiento_de_w_horas_se_mide_en_raiz_de_w(reglas):  # noqa: F811
    """Una caída del 0,5 % en una hora: con 1 h, z ≈ 3,4 (entra); con 4 h, z ≈ 1,8 porque σ se escala con √4 (sin la
    raíz serían 3,7 y entraría)."""
    c = 100 + 0.1 * np.sin(np.arange(N) / 30.0)       # el mismo camino tranquilo, sin la caída del 3 %
    c[K:] = c[K:] * 0.995
    fu = velas_mano(c)
    fu.noticias = Noticias.de_filas([noticia(TD - 2 * H, mon=ETH)], TIPOS, ["a.com"], {"firma": "mano"})
    m = datos.Mercado(fu)
    z1, z4 = z_a_mano(m, 1), z_a_mano(m, 4)
    assert abs(z1) >= 3 > abs(z4) and 2 * abs(z4) >= 3                   # el caso discrimina
    assert senales(m).t.tolist() == [TD]
    assert not len(senales(m, horas_mov=4))
