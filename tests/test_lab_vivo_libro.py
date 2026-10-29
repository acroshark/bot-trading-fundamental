"""El libro del laboratorio vivo (docs/07, 7): da lo mismo que apertura.simulate_plan llamado a mano; una posición por
moneda; topes del día de Madrid; controles del paper; funding en los cortos (la mitad tras la parcial); comisiones por
perfil (tomador y devolución del creador en la parcial y el objetivo); sin X-Perps no hay cortos; stop estructural
subido a 1,5 % y veto por encima del tope; «fin de datos» que ocupa y no cuenta; y unas cifras fijas de un escenario
sintético (el libro es la réplica de medir_selectiva.py, que el canario comprueba con datos reales)."""
import numpy as np
import pytest

from bot import apertura as ap
from bot import comisiones, paper
from bot.lab_vivo import datos, libro, salidas
from bot.lab_vivo.familias.base import Senales, emitir
from test_lab_vivo_comun import MIN, T0, H, reglas  # noqa: F401

H3 = {"tipo": "H", "sl": 1.5, "target_r": 3.0, "max_hold_h": 24}


def fuente_ohlc(filas_por_moneda: dict, t0: int = T0, funding: dict | None = None):
    """Velas exactas: {moneda: [(o, h, l, c), ...]} (una por minuto desde t0)."""
    velas, densos = {}, {}
    for m, filas in filas_por_moneda.items():
        a = np.asarray(filas, float)
        t = t0 + np.arange(len(a), dtype=np.int64) * MIN
        v = datos.Velas(t, a[:, 0], a[:, 1], a[:, 2], a[:, 3], np.full(len(a), 10.0), np.full(len(a), 5.0))
        velas[m], densos[m] = v, datos.denso_de(v)
    mons = tuple(filas_por_moneda)
    fund = {m: (funding or {}).get(m, (np.zeros(0, np.int64), np.zeros(0))) for m in mons}
    n = max(len(x) for x in filas_por_moneda.values())
    per = {"epoca": "prueba", "inicio": t0, "fin": t0 + n * MIN, "corte": t0 + n * MIN}
    return datos.Fuente(mons, per, velas, densos, {}, fund, (np.zeros(0, np.int64), np.zeros(0)), {},
                        (np.zeros(0, np.int64), []), [], "ohlc", None, {})


def camino(cierres, rango=0.0002):
    out, prev = [], cierres[0]
    for c in cierres:
        o = prev
        out.append((o, max(o, c) * (1 + rango), min(o, c) * (1 - rango), c))
        prev = c
    return out


def plano(n, p=100.0):
    return [p] * n


def subida(n_plano=10, n=600, p=100.0, paso=0.004):
    return plano(n_plano, p) + [p * (1 + paso * k) for k in range(1, n - n_plano + 1)]


def bajada(n_plano=10, n=600, p=100.0, paso=0.004):
    return plano(n_plano, p) + [p * (1 - paso * k) for k in range(1, n - n_plano + 1)]


def med(fu, s, salida=H3, perfil="backtest:0.10", **kw):
    return libro.medir(datos.Mercado(fu), s, salida, perfil=perfil, **kw)


def test_igual_que_simulate_plan_a_mano(reglas):  # noqa: F811
    filas = camino(subida())
    fu = fuente_ohlc({"BTC": filas})
    t = T0 + 10 * MIN
    m = med(fu, emitir([t], 0, 1))
    assert len(m.operaciones) == 1
    op = m.operaciones[0]
    with comisiones.costes_de_backtest(0.10):
        E = filas[10][0] * (1 + libro.SLIP)
        plan = ap.impulse_plan(1, E, salidas.params_motor(salidas.normalizar(H3)[0], 1.5), None, "spot")
        bars = [[T0 + k * MIN, *filas[k], 10.0, 0.0, 0.0] for k in range(10, len(filas))]
        rr = ap.simulate_plan(bars, 0, 1, plan, T0 + 10 * MIN + 24 * H, fee=0.001, slip=libro.SLIP)
    assert op["why"] == rr["why"] == "objetivo"
    assert op["ret"] == pytest.approx(rr["ret"], abs=1e-12)
    assert op["R"] == pytest.approx(rr["ret"] / 1.5)


def test_una_posicion_por_moneda_tambien_la_mitad_que_queda(reglas):  # noqa: F811
    fu = fuente_ohlc({"BTC": camino(subida(n=4000, paso=0.0005))})
    A = {"tipo": "A", "sl": 1.5, "suelo": "F", "max_hold_h": 24, "max_hold_active_h": 168}
    t = T0 + 10 * MIN
    m = med(fu, emitir([t, t + 200 * MIN], 0, 1), salida=A)
    ops = m.operaciones + m.abiertas
    assert len(ops) == 1 and ops[0]["partial"]          # la mitad salió en 3R …
    assert m.vetos["moneda ocupada"] == 1               # … y la otra mitad sigue ocupando la moneda


def test_tope_de_6_entradas_del_dia(reglas):  # noqa: F811
    # cada operación gana en pocos minutos (objetivo 3R) y la moneda queda libre para la siguiente
    c = []
    for k in range(7):
        c += plano(5, 100.0) + [100 * (1 + 0.01 * j) for j in range(1, 8)] + plano(3, 100.0)
    fu = fuente_ohlc({"BTC": camino(c + plano(50))})
    ts = [T0 + (k * 15 + 5) * MIN for k in range(7)]
    m = med(fu, emitir(ts, 0, 1))
    assert len(m.operaciones) == 6 and m.vetos["tope de 6 del día"] == 1


def test_pausa_por_perdida_del_dia(reglas):  # noqa: F811
    # pérdidas con hueco (la vela siguiente abre muy por debajo del stop): cada una pierde ≈ 1 % de la cuenta
    filas = []
    for k in range(6):
        filas += [(100, 100.02, 99.98, 100)] * 5 + [(97, 97.1, 96.9, 97)] * 3 + [(100, 100.02, 99.98, 100)] * 2
    fu = fuente_ohlc({"BTC": filas + [(100, 100.02, 99.98, 100)] * 20})
    ts = [T0 + (k * 10 + 4) * MIN for k in range(6)]
    m = med(fu, emitir(ts, 0, 1))
    assert m.vetos["paper detenido por la pérdida del día"] >= 1
    assert len(m.operaciones) <= 4


def test_exposicion_y_4_posiciones(reglas):  # noqa: F811
    p = camino(plano(400))
    t = T0 + 10 * MIN
    fu = fuente_ohlc({c: p for c in ("BTC", "ETH", "SOL", "XRP")})
    m = med(fu, Senales.juntar([emitir([t], i, 1) for i in range(4)]))
    assert m.vetos["exposición > 100 %"] == 1           # con stop de 1,5 % cada una es ≈ 29 % de la cuenta
    fu5 = fuente_ohlc({c: p for c in ("BTC", "ETH", "SOL", "XRP", "ADA")})
    s5 = Senales.juntar([emitir([t], i, 1) for i in range(5)])
    m5 = med(fu5, s5, salida={**H3, "sl": 5.0, "max_hold_h": 24})
    assert m5.vetos["4 posiciones abiertas"] == 1


def test_funding_en_cortos_y_la_mitad_tras_la_parcial(reglas):  # noqa: F811
    c = bajada(n=3000, paso=0.0003)
    ft = np.array([T0 + 60 * MIN, T0 + 300 * MIN, T0 + 2000 * MIN], np.int64)
    fu = fuente_ohlc({"BTC": camino(c)}, funding={"BTC": (ft, np.array([0.001, 0.001, 0.001]))})
    A = {"tipo": "A", "sl": 1.5, "suelo": "F", "max_hold_h": 24, "max_hold_active_h": 168}
    m = med(fu, emitir([T0 + 10 * MIN], 0, -1), salida=A)
    op = (m.operaciones + m.abiertas)[0]
    assert op["inst"] == "perp" and op["partial"]
    esperado = sum(paper.funding_pay("short", 100.0, 0.1) * (0.5 if op["th"] and x > op["th"] else 1.0)
                   for x in ft if op["t_fill"] < x <= op["ext"])
    assert op["fund_pct"] == pytest.approx(esperado) and esperado > 0
    assert op["R_f"] == pytest.approx((op["ret"] + op["fund_pct"]) / op["sl"])


def test_comisiones_por_perfil_tomador_y_creador(reglas):  # noqa: F811
    filas = camino(subida())
    fu = fuente_ohlc({"BTC": filas})
    s = emitir([T0 + 10 * MIN], 0, 1)
    con = med(fu, s, perfil=comisiones.CON_XPERPS).operaciones[0]
    bt = med(fu, s, perfil="backtest:0.10").operaciones[0]
    assert con["why"] == "objetivo"
    E = con["E"]
    tp = E * (1 + 0.015 * 3)
    with libro.perfil_activo(comisiones.CON_XPERPS):
        plan = ap.impulse_plan(1, E, salidas.params_motor(salidas.normalizar(H3)[0], 1.5), None, "spot")
        bars = [[T0 + k * MIN, *filas[k], 10.0, 0.0, 0.0] for k in range(10, len(filas))]
        rr = ap.simulate_plan(bars, 0, 1, plan, T0 + 10 * MIN + 24 * H, fee=0.001, slip=libro.SLIP)
    devol = (0.0010 - 0.0008) * tp / E * 100           # el objetivo sale como creador (0,08 %): se devuelve la diferencia
    assert con["ret"] == pytest.approx(rr["ret"] + devol, abs=1e-12)
    assert bt["ret"] == pytest.approx(rr["ret"], abs=1e-12)   # en el backtest creador = tomador


def test_sin_xperps_no_hay_cortos(reglas):  # noqa: F811
    fu = fuente_ohlc({"BTC": camino(bajada())})
    m = med(fu, emitir([T0 + 10 * MIN], 0, -1), perfil=comisiones.SIN_XPERPS)
    assert not m.operaciones and m.vetos["sin_cortos"] == 1
    m2 = med(fu, emitir([T0 + 10 * MIN], 0, -1), estres="sin_xperps")
    assert m2.vetos["sin_cortos"] == 1


def test_stop_estructural_sube_a_1_5_y_veto_por_encima(reglas):  # noqa: F811
    fu = fuente_ohlc({"BTC": camino(subida(n=3000, paso=0.0005)), "ETH": camino(subida(n=3000, paso=0.0005))})
    est = {**H3, "sl": "estructural", "sl_max": 5.0}
    s = Senales.juntar([emitir([T0 + 10 * MIN], 0, 1, stop_pct=1.0), emitir([T0 + 10 * MIN], 1, 1, stop_pct=7.0)])
    m = med(fu, s, salida=est)
    assert len(m.operaciones) == 1 and m.operaciones[0]["sl"] == pytest.approx(1.5)
    assert m.vetos["stop_lejano"] == 1


def test_fin_de_datos_ocupa_y_no_cuenta(reglas):  # noqa: F811
    fu = fuente_ohlc({"BTC": camino(plano(300))})
    s = emitir([T0 + 10 * MIN, T0 + 100 * MIN], 0, 1)
    m = med(fu, s)
    assert not m.operaciones and len(m.abiertas) == 1 and m.abiertas[0]["why"] == "fin de datos"
    assert m.vetos["moneda ocupada"] == 1


def test_hueco_tras_la_senal_no_entra(reglas):  # noqa: F811
    filas = camino(plano(100))
    fu = fuente_ohlc({"BTC": filas[:20]}, t0=T0)
    fu2 = fuente_ohlc({"BTC": filas[:20]}, t0=T0 + 60 * MIN)
    v = datos.Velas(np.r_[fu.velas["BTC"].t, fu2.velas["BTC"].t], *(np.r_[getattr(fu.velas["BTC"], k), getattr(fu2.velas["BTC"], k)]
                                                                  for k in ("o", "h", "l", "c", "v", "tb")))
    fu.velas["BTC"], fu.densos["BTC"] = v, datos.denso_de(v)
    m = med(fu, emitir([T0 + 25 * MIN], 0, 1))
    assert m.vetos["sin velas (hueco tras la señal)"] == 1


def test_reestres_contrario_y_entrada_tarde(reglas):  # noqa: F811
    fu = fuente_ohlc({"BTC": camino(subida())})
    mm = datos.Mercado(fu)
    s = emitir([T0 + 10 * MIN], 0, 1)
    m = libro.medir(mm, s, H3, perfil="backtest:0.10")
    op = m.operaciones
    contra = libro.reestres(mm, op, H3, "contrario", perfil="backtest:0.10")
    assert contra[0]["d"] == -1 and contra[0]["R_f"] < 0 < op[0]["R_f"]
    tarde = libro.reestres(mm, op, H3, "entrada_1_min_tarde", perfil="backtest:0.10")
    assert tarde[0]["R_f"] < op[0]["R_f"] + 1e-9


def test_cifras_fijas_escenario_sintetico(reglas):  # noqa: F811
    """El libro con las entradas de «6 diarias» (apertura_impulso con SEIS) sobre 60 días sintéticos: cifras fijas.
    Si cambian, ha cambiado la gestión (simulate_plan, impulse_plan…) o el libro: hay que mirar por qué."""
    from bot.lab_vivo import canario
    from bot.lab_vivo.familias import obtener
    fu = datos.fuente_sintetica(dias=60, semilla=5)
    m = datos.Mercado(fu)
    s = obtener("apertura_impulso").senales(m, dict(canario.P_SEIS), fu.periodo["inicio"], fu.periodo["fin"])
    md = libro.medir(m, s, salidas.SalidaRefSeis2(), perfil=canario.PERFIL)
    c = canario.cifras(md.operaciones, fu.periodo["corte"])
    assert (len(s), c["dentro_n"], c["fuera_n"]) == CIFRAS["n"]
    assert c["dentro_R"] == pytest.approx(CIFRAS["dentro_R"], abs=1e-4)
    assert c["fuera_R"] == pytest.approx(CIFRAS["fuera_R"], abs=1e-4)


CIFRAS = {"n": (876, 153, 99), "dentro_R": 0.6802, "fuera_R": 2.662}   # medidas el 29-10-2026 con este libro
# (los datos sintéticos tienen tendencias fuertes a propósito: las cifras no dicen nada del mercado real)
