"""Casos hechos a mano de las familias principales (docs/07, 17): con velas construidas para que la señal se sepa de
antemano, cada familia la da en el minuto y el sentido esperados, una sola vez (por flanco), y con su prueba del
recorte. Las familias nuevas del generador llevan su propio archivo test_lab_vivo_familia_<nombre>.py."""
import numpy as np
import pytest

from bot.lab_vivo import datos
from bot.lab_vivo.familias import obtener
from test_lab_vivo_comun import DAY, H, MIN, T0, reglas, velas_mano  # noqa: F401


def recorte_ok(fam, m, p, a, b, cortes):
    base = fam.senales(m, p, a, b)
    for T in cortes:
        assert base.entre(a, T).iguales(fam.senales(m.recortado(T), p, a, b).entre(a, T))


def test_ruptura_canal_primera_barra_que_rompe(reglas):  # noqa: F811
    n = 5 * 1440
    c = 100 + 0.2 * np.sin(np.arange(n) / 50.0)          # 4 días en un canal estrecho …
    c[4 * 1440 + 30:] = 103.0                            # … y una ruptura que se queda arriba
    m = datos.Mercado(velas_mano(c))
    fam = obtener("ruptura_canal")
    p = {"horas": 48, "tf_min": 60, "stop": "canal", "monedas": ["BTC"]}
    s = fam.senales(m, p, T0, T0 + n * MIN)
    assert len(s) == 1 and s.dir[0] == 1
    assert s.t[0] == T0 + 4 * 1440 * MIN + H            # el cierre de la barra de 1 h que rompe
    assert s.stop_pct[0] == pytest.approx((103 - c[2 * 1440:4 * 1440].min() * (1 - 0.0005)) / 103 * 100, rel=1e-3)
    recorte_ok(fam, m, p, T0, T0 + n * MIN, [T0 + 4 * 1440 * MIN + 61 * MIN, T0 + 4 * 1440 * MIN + 59 * MIN])


def test_tendencia_diaria_una_entrada_mientras_dura(reglas):  # noqa: F811
    dias = 80
    c = np.repeat(100 * 1.01 ** np.arange(dias), 1440)   # sube un 1 % al día
    m = datos.Mercado(velas_mano(c))
    fam = obtener("tendencia_diaria")
    p = {"retorno_d": 14, "umbral_pct": 2.0, "media": "ema50", "monedas": ["ETH"]}
    s = fam.senales(m, p, T0, T0 + dias * DAY)
    assert len(s) == 1 and s.dir[0] == 1 and s.moneda[0] == 1
    assert s.t[0] % DAY == 0                              # decide a las 00:00 UTC con el día cerrado
    recorte_ok(fam, m, p, T0, T0 + dias * DAY, [int(s.t[0]) - MIN, int(s.t[0]) + MIN])


def test_ruptura_volatilidad_williams(reglas):  # noqa: F811
    n = 25 * 1440
    rng = np.random.default_rng(3)
    c = 100 + np.cumsum(rng.normal(0, 0.01, n))          # días tranquilos …
    d = 24 * 1440
    c[d:] = c[d - 1]
    c[d + 300:] = c[d - 1] + 3.0                           # … y el día 25 se dispara a las 05:00 UTC
    m = datos.Mercado(velas_mano(c, rango=0.0))
    fam = obtener("ruptura_volatilidad")
    p = {"k": 0.5, "monedas": ["BTC"]}
    s = fam.senales(m, p, T0 + d * MIN, T0 + n * MIN)
    assert len(s) == 1 and s.dir[0] == 1 and s.t[0] == T0 + (d + 301) * MIN
    recorte_ok(fam, m, p, T0 + d * MIN, T0 + n * MIN, [T0 + (d + 300) * MIN, T0 + (d + 302) * MIN])


def test_calendario_hora_fija(reglas):  # noqa: F811
    c = np.full(6 * 1440, 100.0)
    m = datos.Mercado(velas_mano(c))
    fam = obtener("calendario")
    s = fam.senales(m, {"modo": "hora", "hora": 21}, T0, T0 + 6 * DAY)
    assert len(s) == 6 * 4 and set(((s.t - T0) % DAY).tolist()) == {21 * H}
    assert np.all(s.dir == 1)
    primeras = s.moneda[:4].tolist()
    assert primeras == [0, 1, 2, 3]                       # prioridad: BTC, ETH, SOL, XRP


def test_apertura_impulso_sigue_el_impulso(reglas):  # noqa: F811
    fu = datos.fuente_sintetica(dias=12, semilla=4)
    m = datos.Mercado(fu)
    fam = obtener("apertura_impulso")
    p = {"measure_min": 15, "min_move_pct": 0.0, "entry": "t:15", "monedas": ["BTC"], "windows": ["europa"]}
    s = fam.senales(m, p, fu.periodo["inicio"], fu.periodo["fin"])
    eu = [o["open_ms"] for o in fu.aperturas if o["id"] == "europa"]
    assert len(s) and set((s.t - 15 * MIN).tolist()) <= set(eu)
    den = m.denso("BTC")
    for t, d in zip(s.t.tolist(), s.dir.tolist()):
        a = (t - 15 * MIN - den.t0) // MIN
        mov = den.c[a + 14] / den.o[a] - 1
        assert np.sign(mov) == d                          # a favor del movimiento de los 15 primeros min


def test_momento_serie_estado_cada_dia(reglas):  # noqa: F811
    dias = 140
    rng = np.random.default_rng(5)
    r = rng.normal(0, 0.02, dias)
    r[100:] = 0.03                                        # 40 días de subida fuerte
    c = np.repeat(100 * np.exp(np.cumsum(r)), 1440)
    m = datos.Mercado(velas_mano(c))
    fam = obtener("momento_serie")
    p = {"tf_min": 1440, "L": 7, "z_min": 0.5, "sigma_n": 60, "modo": "estado", "monedas": ["BTC"]}
    s = fam.senales(m, p, T0 + 110 * DAY, T0 + dias * DAY)
    assert len(s) >= 25 and np.all(s.dir == 1)            # una decisión por día mientras dura (el libro veta si ocupa)
    assert len(np.unique(s.t)) == len(s)
    pf = {**p, "modo": "flanco"}
    assert len(fam.senales(m, pf, T0 + 110 * DAY, T0 + dias * DAY)) <= 2
