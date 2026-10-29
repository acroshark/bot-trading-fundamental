"""Hipótesis del laboratorio vivo (docs/07, 5 y 8.1): formato y campos, firma canónica, duplicadas, reglas del usuario
por variante (evolucion.cumple_reglas), plan imposible, fuera de reglas, topes de variantes y filtros, buzón atómico y
motivos de rechazo en español. Y que la siembra y el catálogo inicial convertido pasan la admisión sin datos."""
import copy
import json

import pytest

from bot.lab_vivo import catalogo, hipotesis, salidas
from test_lab_vivo_comun import lab, reglas  # noqa: F401


def base() -> dict:
    return copy.deepcopy(catalogo.siembra()[1])   # ruptura_canal (el ejemplo de 5.2)


def test_siembra_y_catalogo_admitidos(reglas):  # noqa: F811
    hs = catalogo.siembra()
    for h in hs:
        r = hipotesis.validar(h)
        assert r["ok"], (h["id"], r["motivos"])
    cat, pend = catalogo.catalogo(hipotesis_dir())
    if not cat:
        pytest.skip("sin el catálogo del investigador en este equipo")
    malos = {h["id"]: hipotesis.validar(h)["motivos"] for h in cat if not hipotesis.validar(h)["ok"]}
    assert not malos, malos
    assert {"H21", "H37", "H33", "H34", "H35"} <= set(pend)
    assert sum(len(h["variantes"]) for h in cat) >= 190


def hipotesis_dir():
    from bot import config
    return config.DATA_DIR / "lab_vivo" / "cola"


def test_campos_obligatorios_y_motivos_en_espanol(reglas):  # noqa: F811
    h = base()
    del h["logica"]
    r = hipotesis.validar(h)
    assert not r["ok"] and any("falta el campo «logica»" in m for m in r["motivos"])
    h = base()
    h["id"] = "mala id"
    r = hipotesis.validar(h)
    assert any(m.startswith("A1: «id»") for m in r["motivos"])   # un error de formato para la admisión ahí
    h = base()
    h["logica"]["mecanismo"] = "corto"
    r = hipotesis.validar(h)
    assert any(m.startswith("A10: logica.mecanismo") for m in r["motivos"])


def test_firma_canonica():
    p1 = {"horas": 48, "monedas": ["ETH", "BTC"], "x": 1.0000001}
    p2 = {"monedas": ["BTC", "ETH"], "horas": 48.0, "x": 1.00000012}
    s = {"tipo": "A", "sl": "estructural"}
    u = {"monedas": ["XRP", "BTC"], "momento": "continuo"}
    assert hipotesis.firma_variante("f", 1, p1, s, u) == hipotesis.firma_variante("f", 1, p2, s, {**u, "monedas": ["BTC", "XRP"]})
    assert hipotesis.firma_variante("f", 1, p1, s, u) != hipotesis.firma_variante("f", 2, p1, s, u)


def test_valores_por_defecto_cuentan_en_la_firma(reglas):  # noqa: F811
    h = base()
    r1 = hipotesis.validar(h)
    h2 = base()
    h2["variantes"][0]["params"] = {**h2["variantes"][0]["params"], "tf_min": 60}   # el valor por defecto, escrito
    r2 = hipotesis.validar(h2)
    assert r1["variantes"][0]["firma"] == r2["variantes"][0]["firma"]


def test_duplicadas_y_ya_probada(reglas):  # noqa: F811
    h = base()
    r = hipotesis.validar(h)
    firmas = {v["firma"]: f"{h['id']}:{v['vid']}" for v in r["variantes"]}
    h2 = base()
    h2["id"] = "h-20261029-0001-ruptura_canal-otra"
    r2 = hipotesis.validar(h2, firmas)
    assert not r2["ok"] and any(m.startswith("A7: ya probada") for m in r2["motivos"])
    h3 = base()
    h3["id"] = "h-20261029-0002-ruptura_canal-otra"
    h3["variantes"][0]["params"]["horas"] = 168
    r3 = hipotesis.validar(h3, firmas)
    assert r3["ok"] and sum(1 for v in r3["variantes"] if v["duplicada_de"]) == 5


def test_variantes_repetidas_y_tope(reglas):  # noqa: F811
    h = base()
    h["variantes"][1] = {**copy.deepcopy(h["variantes"][0]), "id": "V2"}
    r = hipotesis.validar(h)
    assert any("igual que otra variante" in m for m in r["motivos"])
    h = base()
    h["variantes"].append({**copy.deepcopy(h["variantes"][0]), "id": "V7"})
    r = hipotesis.validar(h)
    assert any(m.startswith("A3: de 1 a 6") for m in r["motivos"]) or any("V1 a V6" in m for m in r["motivos"])


def test_tope_de_filtros(reglas):  # noqa: F811
    h = base()
    h["variantes"][0]["params"].update(regimen_btc="a_favor", volatilidad="alta", funding="no_extremo_en_contra",
                                       dias="mar-jue")
    r = hipotesis.validar(h)
    assert any(m.startswith("A4: V1: 4 filtros") for m in r["motivos"])


def test_plan_imposible_h2r_con_stop_1_5(reglas):  # noqa: F811
    ok, mot, _ = salidas.comprobar_reglas({"tipo": "H", "sl": 1.5, "target_r": 2.0, "max_hold_h": 72})
    assert not ok and "no deja 3 % neto" in mot[0]
    ok2, _, smin = salidas.comprobar_reglas({"tipo": "H", "sl": "estructural", "target_r": 2.0, "max_hold_h": 72})
    assert ok2 and smin > 1.5   # con stop estructural vale desde ≈ 1,62 % (se veta operación a operación)
    h = base()
    h["variantes"][0]["salida"] = {"tipo": "H", "sl": 1.5, "target_r": 2.0, "max_hold_h": 72}
    r = hipotesis.validar(h)
    assert any(m.startswith("A5: V1: no cumple las reglas") for m in r["motivos"])


def test_fuera_de_reglas_marcada(reglas):  # noqa: F811
    h = base()
    h["variantes"][0]["salida"] = {"tipo": "INFO", "max_hold_h": 24}
    r = hipotesis.validar(h)
    assert not r["ok"]
    h["variantes"][0]["fuera_de_reglas"] = True
    r = hipotesis.validar(h)
    assert r["ok"] and r["variantes"][0]["fuera_de_reglas"] and not r["variantes"][0]["cumple_reglas"]
    h2 = base()
    h2["reglas"]["riesgo_pct"] = 1.0
    r2 = hipotesis.validar(h2)
    assert any(m.startswith("A5: «reglas»") for m in r2["motivos"])


def test_familia_desconocida_y_version(reglas):  # noqa: F811
    h = base()
    h["familia"] = "no_existe"
    assert any(m.startswith("A2") for m in hipotesis.validar(h)["motivos"])
    h = base()
    h["familia_version"] = 9
    assert any("versión" in m for m in hipotesis.validar(h)["motivos"])
    h = base()
    r = hipotesis.validar(h, familias_db={"ruptura_canal": {1: "otro_sha"}})
    assert any("familia cambiada sin subir versión" in m for m in r["motivos"])


def test_exploratoria_obligatoria_sin_historia(reglas):  # noqa: F811
    h = base()
    h["datos"] = ["velas_1m", "noticias_vivas"]
    r = hipotesis.validar(h)
    assert any(m.startswith("A6") for m in r["motivos"])


def test_buzon_atomico_no_lee_tmp(lab, reglas):  # noqa: F811
    from bot.lab_vivo.corredor import Corredor
    h = base()
    (lab / "cola" / "entrada" / f"{h['id']}.json.tmp").write_text(json.dumps(h), encoding="utf-8")
    c = Corredor(procesos=1, sin_pausas=True)
    assert c.arrancar()
    assert c.admitir_buzon() == 0
    p = catalogo.escribir([h])[0]
    assert p.exists() and not p.with_suffix(".json.tmp").exists()
    assert c.admitir_buzon() == 1
    assert c.con.execute("SELECT estado FROM hipotesis WHERE id = ?", (h["id"],)).fetchone()[0] == "admitida"
    assert (lab / "cola" / "aceptadas" / f"{h['id']}.json").exists()


def test_rechazo_va_a_rechazadas_con_motivos(lab, reglas):  # noqa: F811
    from bot.lab_vivo.corredor import Corredor
    h = base()
    h["id"] = "h-20261029-0003-ruptura_canal-mala"
    h["variantes"][0]["params"]["horas"] = 7   # fuera del esquema
    catalogo.escribir([h])
    c = Corredor(procesos=1, sin_pausas=True)
    assert c.arrancar()
    c.admitir_buzon()
    d = json.loads((lab / "cola" / "rechazadas" / f"{h['id']}.json").read_text(encoding="utf-8"))
    assert d["admision"]["motivos"] and "valores permitidos" in d["admision"]["motivos"][0]


def test_a_params_motor_solo_apertura_impulso(reglas):  # noqa: F811
    pm, txt = hipotesis.a_params_motor("ruptura_canal", {}, {"tipo": "A", "sl": 2.0})
    assert pm is None and "modelo nuevo" in txt
    pm, txt = hipotesis.a_params_motor("apertura_impulso", {"measure_min": 15, "min_move_pct": 0.0, "entry": "t:15"},
                                       {"tipo": "A", "sl": 2.0, "suelo": "2R", "max_hold_h": 24, "max_hold_active_h": 168})
    assert pm is not None and pm["sl_pct"] == 2.0 and pm["tp1_r"] == 3.0 and pm["floor_r"] == 2.0
