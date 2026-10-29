"""Comisiones de OKX EU dentro del sistema (bot/comisiones.py, 29-10-2026): perfiles, costes y la regla del 3 % neto."""
import pytest

from bot import comisiones as C, config, evolucion, paper


def test_perfiles_de_la_ayuda_oficial_de_okx_eu():
    con, sin = C.PERFILES[C.CON_XPERPS], C.PERFILES[C.SIN_XPERPS]
    assert con["spot"] == {"creador": 0.08, "tomador": 0.10} and con["perp"] == {"creador": 0.02, "tomador": 0.05}
    assert sin["spot"] == {"creador": 0.20, "tomador": 0.35} and sin["perp"] is None
    assert con["cortos"] and not sin["cortos"]
    assert C.POR_DEFECTO == C.CON_XPERPS == C.activo() and config.COMISIONES == C.CON_XPERPS   # lo de hoy


def test_coste_de_ida_y_vuelta_de_la_regla():
    """Tomador a la entrada y a la salida más 2 pb por lado (por el lado seguro: el objetivo, que es límite, paga menos)."""
    assert C.coste_rt_pct() == pytest.approx(0.24) and C.coste_rt_pct("perp") == pytest.approx(0.14)
    assert C.coste_rt_pct("spot", C.SIN_XPERPS) == pytest.approx(0.74)
    assert C.coste_rt_pct("perp", C.SIN_XPERPS) == pytest.approx(0.74)   # sin X-Perps no hay perpetuo: el del contado
    assert C.tasas("perp", C.SIN_XPERPS)["sin_instrumento"] is True
    assert C.tasa("spot", "creador") == 0.08 and C.tasa("perp", "tomador") == 0.05
    assert paper.cost_rt_pct() == C.coste_rt_pct() and paper.be_offset_pct() == pytest.approx(0.26)


def test_elegir_reiniciar_y_configuracion(monkeypatch, caplog):
    assert C.elegir(C.SIN_XPERPS) == (True, None) and C.activo() == C.SIN_XPERPS and not C.hay_cortos()
    assert C.elegir("binance")[0] is False and C.activo() == C.SIN_XPERPS
    C.reiniciar()
    assert C.activo() == C.CON_XPERPS
    monkeypatch.setattr(config, "COMISIONES", "okx_eu_sin_xperps")
    assert C.activo() == C.SIN_XPERPS and C.config_perfil() == C.SIN_XPERPS
    assert C.vista()["aviso_config"] is None
    # Una errata en BOT_COMISIONES no puede hacer que el paper cobre de menos: el más caro, y la web lo avisa
    monkeypatch.setattr(config, "COMISIONES", "okx_eu_sin_xperp")
    assert C.activo() == C.SIN_XPERPS and not C.hay_cortos() and "no es un perfil conocido" in caplog.text
    v = C.vista()
    assert v["config"] == C.SIN_XPERPS and v["aviso_config"].startswith("BOT_COMISIONES=okx_eu_sin_xperp no es un perfil")
    assert "se usa el más caro, «OKX EU sin cuenta X-Perps»" in v["aviso_config"]


def test_la_cuenta_real_manda_y_sin_la_del_contado_no_hay_perfil_cuenta():
    assert not C.poner_cuenta(None)                      # sin comisión del contado: nada
    assert C.poner_cuenta(0.1, 0.08, 0.05, 0.02, cortos=True) and C.activo() == C.CUENTA
    assert not C.poner_cuenta(0.1, 0.08, 0.05, 0.02, cortos=True)   # igual: no cambia nada
    v = C.vista()
    assert v["cuenta"] and v["perfil"] == "cuenta" and v["cortos"] and v["perp"] == {"creador": 0.02, "tomador": 0.05}
    assert C.poner_cuenta(0.35, None, 0.05, 0.02, cortos=False) and not C.hay_cortos()
    assert C.tasas("spot") == {"perfil": "cuenta", "inst": "spot", "creador": 0.35, "tomador": 0.35}
    assert C.quitar_cuenta() and C.activo() == C.CON_XPERPS and not C.quitar_cuenta()


def _cuenta_real(**kw) -> dict:
    """Mensaje cuenta_real de la cuenta de verdad del usuario en solo lectura, con X-Perps."""
    return {"type": "cuenta_real", "modo": "solo_lectura", "demo": False, "comisiones": {"spot": 0.1, "perp": 0.05},
            "comisiones_creador": {"spot": 0.08, "perp": 0.02}, "cortos": {"ok": True}, "xperps": ["BTC", "ETH"], **kw}


def test_la_demo_de_okx_no_cambia_el_perfil_del_paper():
    """La comisión de la demo de OKX (OKX_DEMO=1) no es la de la cuenta del usuario: no pone el perfil «cuenta» y, si
    estaba puesto, lo quita. Tampoco con el modo apagado ni con un mensaje sin «demo»."""
    C.observar(_cuenta_real(modo="demo", demo=True))
    assert C.activo() == C.CON_XPERPS
    C.observar(_cuenta_real(modo="solo_lectura", demo=True))   # demo parada (solo lectura con claves de la demo)
    assert C.activo() == C.CON_XPERPS
    C.observar({k: v for k, v in _cuenta_real().items() if k != "demo"})
    assert C.activo() == C.CON_XPERPS
    C.observar(_cuenta_real())
    assert C.activo() == C.CUENTA
    C.observar(_cuenta_real(modo="demo", demo=True))
    assert C.activo() == C.CON_XPERPS
    C.observar(_cuenta_real(modo="real"))
    assert C.activo() == C.CUENTA
    C.observar(_cuenta_real(modo="apagado"))
    assert C.activo() == C.CON_XPERPS


def test_los_cortos_de_la_cuenta_dependen_de_tener_x_perps_no_del_modo_de_posicion():
    """Con X-Perps pero en long_short_mode (o acctLv 1), el conector no puede abrir cortos, pero el paper sí los mide:
    la cuenta tiene X-Perps. Sin la comisión del perpetuo leída, vale la última leída o la oficial con X-Perps, en vez
    de quitar los cortos a ratos."""
    C.observar(_cuenta_real(cortos={"ok": False, "por_que": "Sin cortos: el modo de posición es long_short_mode"},
                            comisiones={"spot": 0.1, "perp": None}, comisiones_creador={"spot": 0.08, "perp": None}))
    assert C.activo() == C.CUENTA and C.hay_cortos()
    assert C.tasas("perp") == {"perfil": "cuenta", "inst": "perp", "creador": 0.02, "tomador": 0.05}   # la oficial
    assert C.perfil()["por_que"] == "Cortos en X-Perps: BTC, ETH"
    C.observar(_cuenta_real(comisiones={"spot": 0.1, "perp": 0.04}, comisiones_creador={"spot": 0.08, "perp": 0.015}))
    assert C.tasas("perp")["tomador"] == 0.04
    C.observar(_cuenta_real(comisiones={"spot": 0.1, "perp": None}, comisiones_creador={"spot": 0.08, "perp": None}))
    assert C.hay_cortos() and C.tasas("perp")["tomador"] == 0.04 and C.tasas("perp")["creador"] == 0.015   # la última
    C.observar(_cuenta_real(xperps=[], cortos={"ok": False, "por_que": "Sin cortos: tu cuenta no tiene X-Perps"}))
    assert C.activo() == C.CUENTA and not C.hay_cortos()                                     # sin X-Perps, sin cortos
    C.observar(_cuenta_real(xperps=None, cortos={"ok": True}))   # un conector sin «xperps»: vale cortos.ok
    assert C.hay_cortos()


def test_vista_para_la_web():
    v = C.vista()
    assert v["perfil"] == C.CON_XPERPS and v["nombre"] == "OKX EU con cuenta X-Perps" and v["cortos"] is True
    assert v["texto"] == "contado 0,08 % creador / 0,10 % tomador; X-Perps 0,02 % / 0,05 %; cortos en X-Perps"
    assert "Sin cuenta X-Perps en OKX EU no hay cortos y el contado cuesta 0,35 %" in v["aviso"]
    assert [p["id"] for p in v["perfiles"]] == [C.CON_XPERPS, C.SIN_XPERPS]
    assert v["perfiles"][1]["texto"] == "contado 0,20 % creador / 0,35 % tomador; sin cortos"
    assert v["backtests"]["comision_pct"] == config.FEE_PCT and set(v["tipos"]) == {"tomador", "creador"}


def test_cumple_reglas_usa_el_perfil_en_uso():
    """1,5 % × 2,4R = 3,6 % bruto: con X-Perps deja 3,36 % neto; sin X-Perps (0,74 % de ida y vuelta), 2,86 %."""
    params = {"sl_pct": 1.5, "target_r": 2.4}
    assert evolucion.cumple_reglas(params)[0]
    C.elegir(C.SIN_XPERPS)
    ok, fallos = evolucion.cumple_reglas(params)
    assert not ok and any("2,86 % neto" in f for f in fallos)
    assert evolucion.cumple_reglas({"sl_pct": 1.5, "target_r": 3.0})[0]   # «6 diarias»: 4,5 − 0,74 = 3,76 %
    from bot import apertura as ap
    assert evolucion.cumple_reglas({k: v for k, v in ap.SEIS2.items() if k not in ("estrategia", "lab")})[0]


def test_regla_del_paper_por_instrumento(monkeypatch):
    monkeypatch.setattr(config, "USER_RULES", True)
    t = {"stopPct": 1.5, "tp1R": 2.05, "tp1Frac": 0.5, "lockR": None, "instrument": "perp"}
    assert paper.exit_rules(t)["tp1_frac"] == 0.0                           # 3,075 − 0,14 = 2,94 %: no llega…
    t["tp1R"] = 2.1                                                         # 3,15 − 0,14 = 3,01 %: en X-Perps sí
    assert paper.exit_rules(t)["tp1_frac"] == 0.5
    assert paper.exit_rules({**t, "instrument": "spot"})["tp1_frac"] == 0.0   # al contado (0,24 %), 2,91 %: no
