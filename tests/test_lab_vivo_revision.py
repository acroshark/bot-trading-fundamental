"""Los arreglos de la revisión del 29-10-2026 (docs/07, 20.8), con velas sintéticas o hechas a mano y sin red:
- el N acumulado fuera de lab_vivo.db (registro_n.py): una base con menos pruebas no mide y la herencia la acepta el
  usuario;
- el N efectivo con su suelo por hipótesis (Li y Ji satura con miles de series de ≈ 411 días) y el aviso;
- estado.json no recalcula las tarjetas en las pausas de las aperturas;
- el canario de las comisiones reales (la fórmula independiente de bruto_neto.py y la tabla de comisiones.py);
- las órdenes que calculan (validar, consolidar, canario) con prioridad baja y sin empezar en una pausa;
- los cierres por tiempo antes del cerrojo, contados."""
import json
from datetime import datetime

import numpy as np
import pytest

from bot import comisiones
from bot.lab_vivo import __main__ as orden
from bot.lab_vivo import ajustes, almacen, canario, catalogo, libro, medidas, registro_n, salida_web, salidas
from bot.lab_vivo.corredor import Corredor
from bot.lab_vivo.familias.base import Senales, emitir
from test_lab_vivo_comun import DAY, MIN, T0, lab, reglas  # noqa: F401
from test_lab_vivo_libro import camino, fuente_ohlc, plano

MAD = ajustes.MAD


def ms(y, mo, d, h=0, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=MAD).timestamp() * 1000)


def corredor(now=None, openings_on=None, sin_pausas=True):
    now = now or [ms(2026, 10, 29, 12)]
    c = Corredor(procesos=1, sin_pausas=sin_pausas, reloj=lambda: now[0], lanzador=lambda tid: {"popen": None, "pid": None},
                 memoria=lambda: 8000.0, cpu=lambda: 5.0, openings_on=openings_on or (lambda d: []))
    assert c.arrancar()
    return c


def _medidas(c, n: int, hip: str = "h-20261029-0000-x") -> None:
    """n variantes medidas en la etapa rápida (cuentan 1 en N cada una), de n hipótesis."""
    with c.con:
        for k in range(n):
            almacen.insertar_prueba(c.con, {"variante": f"{hip}{k:03d}:V1", "etapa": "rapida", "cuenta_en_n": 1,
                                            "n_dentro": 150, "R_dentro": 0.01, "pasa": 0, "motivos": [], "metricas": {}})


# ---------------------------------------------------------------- el N acumulado fuera de la base

def test_una_base_con_menos_pruebas_no_mide_hasta_que_el_usuario_lo_acepta(lab, reglas):  # noqa: F811
    c = corredor()
    _medidas(c, 3)
    c.consolidar()
    mx = registro_n.maximo()
    assert mx["n_acumulado"] == 3 and mx["n_lab"] >= 3 and mx["motivo"] == "consolidación"
    c.con.close()
    for sufijo in ("", "-wal", "-shm"):                   # se borra lab_vivo.db (o se cambia por otra)
        p = ajustes.DB.with_name(ajustes.DB.name + sufijo)
        if p.exists():
            p.unlink()
    c2 = corredor()
    assert c2.estado == "error" and c2.bloqueo_n and "n_acumulado.jsonl" in c2.motivo and "heredar-n" in c2.motivo
    c2.paso()
    assert not c2.con.execute("SELECT COUNT(*) FROM trabajos").fetchone()[0]   # no planifica ni mide nada
    e = json.loads(ajustes.ESTADO.read_text(encoding="utf-8"))
    assert e["estado"] == "error" and "n_acumulado.jsonl" in e["motivo"]
    ajustes.HEREDAR_N.write_text(json.dumps({"nota": "rehecha a propósito", "por": "usuario"}), encoding="utf-8")
    c2.paso()                                              # la orden heredar-n con el corredor en marcha
    assert not ajustes.HEREDAR_N.exists() and not c2.bloqueo_n and c2.estado == "activo"
    fila = c2.con.execute("SELECT fuente, n, n_efectivo, nota FROM pruebas_previas WHERE fuente LIKE "
                          "'lab_vivo_anterior%'").fetchone()
    assert fila[1] == 3 and fila[2] >= 3 and "rehecha a propósito" in fila[3]
    st = c2.estadistica_global()
    assert st["bh_m"] == 3917 + 3 and st["n_heredado"] >= 3                    # cuentan en BH y en N_dsr
    assert st["n_dsr"] == pytest.approx(st["n_efectivo"] + 381 + 1917 + st["n_heredado"])
    assert c2.con.execute("SELECT COUNT(*) FROM eventos WHERE tipo = 'herencia_n'").fetchone()[0] >= 1
    assert registro_n.comprobar(c2.con) is None


def test_el_registro_solo_apunta_cuando_crece_el_n(lab, reglas):  # noqa: F811
    c = corredor()
    n0 = len(registro_n.leer())
    _medidas(c, 2)
    with c.con:
        assert registro_n.apuntar(c.con, "medida") is not None
        assert registro_n.apuntar(c.con, "medida") is None          # el mismo N: no se repite
    assert len(registro_n.leer()) == n0 + 1


# ---------------------------------------------------------------- N efectivo: el suelo por hipótesis

def test_n_efectivo_bloques_no_satura():
    rng = np.random.default_rng(4)
    T = 40
    series = {f"h{k}": [rng.normal(0, 1, T) for _ in range(3)] for k in range(60)}   # 180 series independientes
    todas = [s for v in series.values() for s in v]
    lij = medidas.n_efectivo(todas)
    assert lij <= 2 * T                                    # Li y Ji no pasa de 2 × días (rango ≤ T): satura
    assert medidas.n_efectivo_bloques(series) > 120        # por hipótesis, casi las 180
    a = rng.normal(0, 1, T)
    casi = {"h": [a, a + 1e-9 * rng.normal(0, 1, T), a * 2]}
    assert medidas.n_efectivo_bloques(casi) < 1.5                                     # tres casi iguales: una


def test_estadistica_global_usa_el_suelo_y_avisa(lab, reglas):  # noqa: F811
    c = corredor()
    rng = np.random.default_rng(5)
    with c.con:
        for k in range(40):
            for v in ("V1", "V2"):
                vid = f"h-20261029-0000-s{k:02d}:{v}"
                almacen.insertar_prueba(c.con, {"variante": vid, "etapa": "rapida", "cuenta_en_n": 1, "n_dentro": 150,
                                                "R_dentro": 0.01, "pasa": 1})
                almacen.insertar_prueba(c.con, {"variante": vid, "etapa": "completa", "cuenta_en_n": 0, "n_fuera": 120,
                                                "R_fuera": 0.01, "p_fuera": 0.4, "sr_fuera": 0.02, "dias_fuera": 30,
                                                "asimetria": 0.0, "curtosis": 3.0, "pasa": 0},
                                        ("2025-08-16", rng.normal(0, 1, 30)))
        for k in range(5):                                 # hipótesis que no pasaron la rápida: sin fuera de muestra
            almacen.insertar_prueba(c.con, {"variante": f"h-20261029-0000-r{k}:V1", "etapa": "rapida", "cuenta_en_n": 1,
                                            "n_dentro": 150, "R_dentro": -0.01, "pasa": 0})
    st = c.estadistica_global()
    assert st["n_efectivo_lij"] <= 2 * 30                  # satura con 30 días (rango ≤ 30)
    assert st["n_efectivo_hipotesis"] >= 40 + 5            # al menos una por hipótesis
    assert st["n_efectivo"] == st["n_efectivo_hipotesis"] > st["n_efectivo_lij"]
    assert st["aviso_n"] and "satura" in st["aviso_n"]
    cons = c.consolidar()
    assert cons["aviso_n"] and cons["n_efectivo_hipotesis"] == st["n_efectivo_hipotesis"]
    e = json.loads(ajustes.ESTADO.read_text(encoding="utf-8"))
    assert e["consolidacion"]["aviso_n"] and e["consolidacion"]["n_dsr"] == pytest.approx(cons["n_dsr"])


# ---------------------------------------------------------------- estado.json en las pausas

def test_en_la_pausa_no_se_recalculan_las_tarjetas(lab, reglas, monkeypatch):  # noqa: F811
    from datetime import date
    ab = ms(2026, 10, 29, 9, 0)
    now = [ab - 60 * MIN]
    c = corredor(now, openings_on=lambda d: [{"id": "europa", "open_ms": ab}] if d == date(2026, 10, 29) else [],
                 sin_pausas=False)
    llamadas = []
    real = salida_web.pesado
    monkeypatch.setattr(salida_web, "pesado", lambda con: llamadas.append(1) or real(con))
    c.actualizar_agenda(now[0])
    c.escribir_salidas()
    assert len(llamadas) == 1                              # fuera de la pausa, con cambios: se calculan
    now[0] = ab + 5 * MIN                                  # en la pausa (08:55-09:30)
    c.cambios = True
    for _ in range(3):
        c.escribir_salidas()
    assert len(llamadas) == 1                              # en la pausa, el latido con las últimas tarjetas
    e = json.loads(ajustes.ESTADO.read_text(encoding="utf-8"))
    assert e["latido"] and "mejores" in e and c.cambios    # el cambio queda para después de la pausa
    now[0] = ab + 31 * MIN
    c.escribir_salidas()
    assert len(llamadas) == 2 and not c.cambios


# ---------------------------------------------------------------- canario de las comisiones reales

def _ops_a_mano(salida: dict):
    """Cuatro operaciones con velas hechas a mano: largo que llega al objetivo (o a la parcial y al seguimiento), corto
    igual, largo que salta el stop y largo que sale por tiempo."""
    n = 1700
    sube = [100.0] * 10 + [100 * (1 + 0.003 * k) for k in range(1, 61)]
    sube += [sube[-1] * (1 - 0.003 * k) for k in range(1, n - len(sube) + 1)]
    baja = [100.0] * 10 + [100 * (1 - 0.003 * k) for k in range(1, 61)]
    baja += [baja[-1] * (1 + 0.003 * k) for k in range(1, n - len(baja) + 1)]
    cae = [100.0] * 10 + [100 * (1 - 0.003 * k) for k in range(1, n - 9)]
    fu = fuente_ohlc({"BTC": camino(sube), "ETH": camino(baja), "SOL": camino(cae), "XRP": camino(plano(n))})
    t = T0 + 10 * MIN
    # el de XRP, cuando ya han salido los otros tres (si no, la exposición pasaría del 100 % y no entraría)
    s = Senales.juntar([emitir([t], 0, 1), emitir([t], 1, -1), emitir([t], 2, 1), emitir([t + 190 * MIN], 3, 1)])
    from bot.lab_vivo import datos
    m = datos.Mercado(fu)
    med = libro.medir(m, s, salida, perfil=comisiones.CON_XPERPS)
    return m, med.operaciones


@pytest.mark.parametrize("salida", [
    {"tipo": "H", "sl": 1.5, "target_r": 3.0, "max_hold_h": 24},
    {"tipo": "A", "sl": 1.5, "suelo": "F", "trail_r": 2.0, "max_hold_h": 24, "max_hold_active_h": 168}])
def test_canario_de_comisiones_cuadra_con_la_formula_independiente(reglas, salida):  # noqa: F811
    m, ops = _ops_a_mano(salida)
    assert len(ops) == 4
    r = canario.comprobar_comisiones(m, ops, salidas.Salida(salida))
    assert r["ok"], r
    assert r["n"] == 4 and r["cortos"] == 1 and r["max_dif"] <= canario.TOL_COMISION and not r["tabla"]
    assert {"inicial", "tiempo"} <= set(r["por_salida"])
    assert "objetivo" in r["por_salida"] or any(o["partial"] for o in ops)
    assert "cobran lo mismo" in r["texto"]


def test_canario_de_comisiones_caza_un_libro_que_cobra_mal(reglas, monkeypatch):  # noqa: F811
    salida = {"tipo": "H", "sl": 1.5, "target_r": 3.0, "max_hold_h": 24}
    m, ops = _ops_a_mano(salida)
    real = libro.tasas
    monkeypatch.setattr(libro, "tasas", lambda inst: (real(inst)[0], real(inst)[1] * 0.5))   # creador mal cobrado
    r = canario.comprobar_comisiones(m, ops, salidas.Salida(salida))
    assert not r["ok"] and r["peores"] and "NO cuadra" in r["texto"]


def test_canario_de_comisiones_caza_otra_tabla(reglas, monkeypatch):  # noqa: F811
    assert canario.comprobar_tabla() == []
    tabla = {k: dict(v) for k, v in canario.TABLA.items()}
    tabla[comisiones.CON_XPERPS]["spot"] = (0.10, 0.07)
    monkeypatch.setattr(canario, "TABLA", tabla)
    f = canario.comprobar_tabla()
    assert f and "okx_eu_con_xperps spot" in f[0]


# ---------------------------------------------------------------- cierres por tiempo antes del cerrojo

def test_cierres_por_tiempo_antes_del_cerrojo():
    ops = [{"why": "tiempo", "locked": False, "R_f": 0.5}, {"why": "tiempo", "locked": False, "R_f": -0.4},
           {"why": "tiempo", "locked": True, "R_f": 2.4}, {"why": "objetivo", "locked": False, "R_f": 3.0},
           {"why": "tiempo", "locked": False, "R_f": -1.05}]
    r = medidas.cierres_por_tiempo(ops)
    assert r == {"n": 3, "de": 5, "pct": 60.0, "entre": 2, "ganan_menos_2R": 1}
    ex = salida_web.excepcion_tiempo({"tipo": "A", "max_hold_h": 24.0}, r)
    assert ex["tope_sin_activar_h"] == 24.0 and "6 diarias" in ex["texto"] and "3 de 5" in ex["texto"]
    assert salida_web.excepcion_tiempo({"tipo": "INFO", "max_hold_h": 24.0}) is None


# ---------------------------------------------------------------- órdenes: prioridad baja y pausas

def test_validar_en_una_pausa_deja_a8_al_corredor(lab, reglas, monkeypatch, capsys):  # noqa: F811
    from bot.lab_vivo import datos, etapas
    car = ajustes.CACHE / f"{datos.epoca_de()}-x"
    car.mkdir(parents=True, exist_ok=True)
    (car / "meta.json").write_text("{}", encoding="utf-8")
    h = catalogo.siembra()[1]
    p = ajustes.TMP / "h.json"
    p.write_text(json.dumps(h, ensure_ascii=False), encoding="utf-8")
    bajo = []
    monkeypatch.setattr(orden, "_bajo", lambda: bajo.append(1))
    monkeypatch.setattr(orden, "_pausa_de_aperturas", lambda: {"nombre": "Europa", "desde": ms(2026, 10, 29, 8, 55),
                                                               "hasta": ms(2026, 10, 29, 9, 30)})
    monkeypatch.setattr(etapas, "admision", lambda *a, **k: pytest.fail("en la pausa no se calcula A8"))
    assert orden.main(["validar", str(p)]) == 0
    out = capsys.readouterr().out
    assert bajo and "pausa por la apertura de Europa" in out and "los hará el corredor" in out and "VALE" in out


def test_consolidar_con_el_corredor_en_marcha_se_la_pide_a_el(lab, reglas, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(orden, "_bajo", lambda: None)
    monkeypatch.setattr(orden, "_pausa_de_aperturas", lambda: None)
    monkeypatch.setattr(orden, "_corredor_vivo", lambda: 4321)
    assert orden.main(["consolidar"]) == 0
    assert ajustes.CONSOLIDAR_AHORA.exists() and "la hará él" in capsys.readouterr().out
    c = corredor()
    c.paso()                                               # el corredor la hace (fuera de las pausas) y borra la orden
    assert not ajustes.CONSOLIDAR_AHORA.exists() and almacen.kv_get(c.con, "consolidacion")
    monkeypatch.setattr(orden, "_pausa_de_aperturas", lambda: {"nombre": "Europa", "desde": 0, "hasta": 0})
    assert orden.main(["canario"]) == ajustes.CODIGO_PAUSADO   # en una pausa no se empieza


def test_heredar_n_sin_corredor(lab, reglas, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(orden, "_corredor_vivo", lambda: None)
    c = corredor()
    _medidas(c, 2)
    c.consolidar()
    c.con.close()
    ajustes.DB.unlink()
    for sufijo in ("-wal", "-shm"):
        q = ajustes.DB.with_name(ajustes.DB.name + sufijo)
        if q.exists():
            q.unlink()
    assert orden.main(["heredar-n", "--nota", "prueba"]) == 0
    assert "Heredadas 2 variantes" in capsys.readouterr().out
    c2 = corredor()
    assert c2.estado == "activo" and not c2.bloqueo_n
