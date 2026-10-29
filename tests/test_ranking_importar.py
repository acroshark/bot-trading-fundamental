"""scripts/ranking_importar.py: los estudios ya hechos pasan a filas comunes del ranking (una por archivo), netas de costes
y con su perfil; E2 descartada por el usuario; la Ruptura V3 de las selectivas va en la fila del motor; Holm dentro de
cada estudio; lo repetido no se duplica; idempotente. Con datos sintéticos en tmp_path: sin red y sin leer
M:\\BotFundamental-datos."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

from bot import ranking

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ranking_importar", ROOT / "scripts" / "ranking_importar.py")
ri = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ri)


def curva(rs: list[float]) -> list:
    out, acc = [], 0.0
    for i, r in enumerate(rs):
        acc += r
        out.append([1_755_300_000_000 + i * 3_600_000, round(acc, 4)])
    return out


RS = [1.0, -1.0, 2.0, -1.0, 3.0, -1.0, -1.0, 0.5, 4.0, -1.0]


def motor(nombre: str, rs: list[float], R: float, op_jornada=None) -> dict:
    fu = {"n": len(rs), "mean_R": R, "paper_R_funding": R, "win_rate": 0.4, "curva_R": curva(rs)}
    if op_jornada is not None:
        fu.update(op_jornada=op_jornada, antes_de_costes={"R_bruto": R + 0.1, "cuenta_jornada_neto_pct": 0.0826})
    return {"generated": 1_793_230_069_373, "nombre": nombre, "fuera": fu,
            "dentro": {"n": 20, "mean_R": -0.03, "paper_R_funding": -0.033, "win_rate": 0.37}}


def tramo_sel(n, R, op=0.4, pct=0.08, gana=50.0) -> dict:
    return {"n": n, "op_dia_con_aperturas": op, "op_dia_natural": op * 306 / 431, "gana_pct": gana, "R_medio": R,
            "R_medio_funding": R, "cuenta_por_jornada_pct": pct, "cuenta_por_dia_natural_pct": pct * 306 / 431,
            "caida_max_cuenta_pct": 4.2}


def salida_sel(p, R=0.3, ops=True) -> dict:
    s = {"salida": "A", "dentro": tramo_sel(100, -0.05), "fuera": tramo_sel(50, R),
         "base_emparejada": {"fuera": {"p_valor": p}}, "fuera_mitades": {"primera": {"R": 0.2}, "segunda": {"R": 0.4}}}
    if ops:
        s["operaciones"] = ([{"R": r, "muestra": "fuera"} for r in RS] + [{"R": 9.0, "muestra": "dentro"}])
    return s


SELECTIVA = {"generado": "2026-10-29T02:43:41+01:00", "jornadas": {"dentro": 468, "fuera": 306},
             "estrategias": {
                 "murphy": {"variantes": {"V1": {"reglas": {}, "salida_A": salida_sel(0.01),
                                                 "salida_H6": salida_sel(0.2, R=0.1, ops=False)}}},
                 "fibo": {"variantes": {"V3": {"salida_FIB": salida_sel(0.5, R=-0.1),
                                               "salida_H6": {"igual_que": "V1", "nota": "la misma"}}}},
                 "ruptura": {"variantes": {"V3": {"salida_A": salida_sel(0.09, R=0.4286)}}}}}


def test_laboratorio_las_que_no_pierden_las_prometedoras_y_e2():
    prom = "z:0|ninguna|todos|ninguno|asia|SOL|todos|pb:15|3|2|sig"
    lab = {"generated": 1_793_034_145_937, "summary": {"tested": 5}, "frozen": {"date": "2026-10-26"},
           "period": {"from": "2023-11-01", "to": "2026-10-23", "split": "2025-08-16"},
           "promising": [{"id": prom, "trades_oos": [{"R": r} for r in RS]}],
           "all": [{"id": "x:0|ninguna|todos|ninguno|todas|todas|todos|t:30|1,5|2|sig", "family": "cadena",
                    "class": "descartada", "is": {"trades": 400, "mean_R": -0.02, "win_rate": 0.3},
                    "oos": {"trades": 309, "mean_R": 0.1, "win_rate": 0.42, "p_day": 0.2, "mean_R_wo_best_month": 0.04},
                    "q": 0.5, "p_base": 0.1},
                   {"id": "y|2|2,5|7d", "oos": {"trades": 50, "mean_R": -0.2}, "is": {}},
                   {"id": prom, "class": "prometedora", "oos": {"trades": 10, "mean_R": -0.05}, "is": {"trades": 9}},
                   {"id": ri.LAB_REF, "class": "prometedora", "oos": {"trades": 278, "mean_R": 0.14}, "q": 1.0, "is": {}},
                   {"id": "w|2|2|24h", "oos": {"trades": 0, "mean_R": None}, "is": {}}]}
    nombres = {lab["all"][0]["id"]: {"sl": 1.5, "r": 2.0, "name": "Bolsas previas en cadena: sigue a la última · "
                                     "entra a +30 min · sin confirmación extra · todas las aperturas · SL 1,5 % · "
                                     "TP 2R (3 %) · hasta la misma apertura del día siguiente"}}
    filas = {f["id"]: f for f in ri.filas_laboratorio(lab, nombres)}
    assert set(filas) == {f"lab2000:{lab['all'][0]['id']}", f"lab2000:{prom}", f"lab2000:{ri.LAB_REF}"}
    x = filas[f"lab2000:{lab['all'][0]['id']}"]
    assert x["nombre"] == ("Bolsas previas en cadena: sigue a la última · entra a +30 min · SL 1,5 % · TP 2R · hasta la "
                           "apertura siguiente")
    assert x["cumple_reglas"] is False   # 1,5 % × 2R = 3 % bruto: deja 2,76 % neto, menos del 3 %
    assert x["fuera"]["jornadas"] == 309 and x["op_dia"] == 1.0 and x["pct_dia"] == 0.05
    assert x["robustez"]["corregida"]["pasa"] is False and x["perfil_costes"] == "laboratorio" and x["probadas"] == 5
    assert x["fecha"] == "2026-10-26" and x["fuera"]["gana_pct"] == 42.0
    e2 = filas[f"lab2000:{ri.LAB_REF}"]
    assert e2["estado"] == "descartada_por_el_usuario" and e2["cumple_reglas"] is True and "E2" in e2["nota"]
    z = filas[f"lab2000:{prom}"]
    assert z["robustez"]["sin_5_mayores"] == -1.0 and z["robustez"]["mitades"] == [0.8, 0.3] and z["caida_max_pct"] > 0
    assert not ranking.entra(ranking.normalizar(z)) and "prometedora" in z["nota"]


def test_replicas_del_motor_con_su_curva_y_la_ruptura_v3_de_las_selectivas():
    seis2 = ri.filas_motor("seis2", motor("6 diarias v2", RS, -0.0743))
    assert seis2["id"] == "seis2" and seis2["estudio"] == "seis2" and seis2["origen"] == "estudio"
    assert seis2["robustez"]["sin_5_mayores"] == -1.0 and seis2["robustez"]["mitades"] == [0.8, 0.3]
    assert seis2["fuera"]["jornadas"] == 306 and seis2["op_dia"] == round(10 / 306, 4)
    assert seis2["pct_dia"] == round(-0.0743 * 0.5 * 10 / 306, 4) and seis2["cumple_reglas"] is True
    assert seis2["caida_max_pct"] == ri.caida_cuenta(RS) and seis2["perfil_costes"] == "backtest"
    _, aparte = ri.filas_selectiva(SELECTIVA)
    bn = {"estrategias": {"ruptura_v3": {"casos": {
        "backtest": {"fuera": {"n": 10, "R_medio": 0.4285, "op_por_jornada": 0.386, "cuenta_por_jornada_pct": 0.0826}},
        "paper_hoy": {"fuera": {"n": 10, "R_medio": 0.4675, "op_por_jornada": 0.386, "cuenta_por_jornada_pct": 0.0901}},
        "sin_xperps": {"fuera": {"n": 3, "R_medio": -0.079, "cuenta_por_jornada_pct": -0.0036}}}}}}
    r3 = ri.filas_motor("ruptura3", motor("Ruptura V3", RS, 0.4285, op_jornada=0.386), aparte, bn)
    assert r3["op_dia"] == 0.386 and r3["pct_dia"] == 0.0826 and r3["fuera"]["R_bruto"] == 0.5285
    assert r3["robustez"]["base_p"] == 0.09 and r3["robustez"]["corregida"]["valor"] == 0.27   # Holm entre las 4
    assert r3["robustez"]["mitades"] == [0.2, 0.4] and r3["caida_max_pct"] == 4.2 and r3["probadas"] == 4
    assert r3["perfiles"]["okx_eu_con_xperps"]["R"] == 0.4675 and r3["perfiles"]["okx_eu_sin_xperps"]["n"] == 3
    e2 = ri.filas_motor("e2_impulso_7d", motor("x", RS, 0.375), e2_q=1.0)
    assert e2["estudio"] == "impulso_7d" and e2["estado"] == "descartada_por_el_usuario"
    assert e2["fuera"]["jornadas"] == 309 and e2["nombre"] == "x" and "eligió después" in e2["nota"]
    # su salida de 7 días se eligió después de ver los datos: frágil y sin corrección, aunque gane fuera y dentro
    assert e2["fragil"] is True and e2["fragil_motivos"] == [ri.E2_7D_MOTIVO]
    assert e2["robustez"]["corregida"] == {"tipo": "sin_p", "valor": None, "pasa": False, "metodo": (
        "sin corrección: la salida de 7 días se eligió después de ver los datos (con la salida del laboratorio, E2 tiene q "
        "1,00 entre las 2.000)")}
    n = ranking.normalizar(e2)
    assert n["fragil"] and n["fragil_motivos"][-2:] == ["sin corrección por número de pruebas", ri.E2_7D_MOTIVO]
    assert "q 1,00" not in ri.filas_motor("e2_impulso_7d", motor("x", RS, 0.375))["robustez"]["corregida"]["metodo"]
    assert seis2["robustez"]["corregida"] is None and "fragil" not in seis2   # una sola configuración, la del preset
    assert ri.filas_motor("seis", None) is None and ri.filas_motor("seis", {"fuera": None}) is None


def test_selectivas_holm_entre_las_del_estudio_y_sin_repetir():
    filas, aparte = ri.filas_selectiva(SELECTIVA)
    por = {f["id"]: f for f in filas}
    assert set(por) == {"selectiva:murphy:V1:A", "selectiva:murphy:V1:H6", "selectiva:fibo:V3:FIB"}
    assert aparte["ruptura3"]["id"] == "selectiva:ruptura:V3:A"   # la del motor: no va como fila aparte
    m = por["selectiva:murphy:V1:A"]
    assert m["robustez"]["corregida"] == {"tipo": "holm", "metodo": "Holm entre las 4 configuraciones del estudio (p "
                                                                    "frente a la base emparejada)", "valor": 0.04,
                                          "pasa": True}
    assert m["robustez"]["sin_5_mayores"] == -1.0 and m["fuera"]["p"] == 0.01 and m["op_dia"] == 0.4
    assert m["pct_dia"] == 0.08 and m["caida_max_pct"] == 4.2 and m["probadas"] == 4 and m["cumple_reglas"] is True
    assert por["selectiva:murphy:V1:H6"]["robustez"]["sin_5_mayores"] is None   # sin operaciones en el archivo
    assert "extensión del 161,8 %" in por["selectiva:fibo:V3:FIB"]["regla"]


def test_curso_toda_la_tabla_menos_la_referencia():
    d = {"generado": "2026-10-29T05:43:51+01:00", "jornadas": {"fuera": 306},
         "tabla": [{"tecnica": "MC", "uso": "(a) estrategia propia", "nombre": "MC-V1_A",
                    "dentro": tramo_sel(300, 0.01), "fuera": tramo_sel(200, 0.05, op=0.65, pct=0.016)},
                   {"tecnica": "MC", "uso": "(b) filtro sobre «6 diarias v2» (libro)", "nombre": "MC-V1",
                    "dentro": tramo_sel(1000, -0.05), "fuera": tramo_sel(900, -0.06, op=3.0, pct=-0.09)},
                   {"tecnica": "MC", "uso": "(b) esperar la señal (T0 + 15 min → T0 + 4 h)", "nombre": "MC-V1",
                    "dentro": tramo_sel(60, 0.2), "fuera": tramo_sel(63, 0.009, op=0.2, pct=0.0009)},
                   {"tecnica": "seis2", "uso": "referencia", "nombre": "«6 diarias v2»", "dentro": {}, "fuera": {}}],
         "a_estrategias_propias": {"MC": {"V1_A": {"base_emparejada": {"fuera": {"p_valor": 0.02}},
                                                   "operaciones": [{"R": r, "muestra": "fuera"} for r in RS]}}},
         "b_filtros": {"MC-V1": {"fuera": {"p": 0.3}}},
         "b_esperas": {"MC": {"V1": {"base_emparejada": {"fuera": {"p_valor": 0.6}},
                                     "fuera_mitades": {"primera": {"R": 0.1}, "segunda": {"R": -0.1}}}}}}
    filas = {f["id"]: f for f in ri.filas_curso(d)}
    assert set(filas) == {"curso:a:MC-V1_A", "curso:b_filtro:MC-V1", "curso:b_espera:MC-V1"}
    a = filas["curso:a:MC-V1_A"]
    assert a["nombre"] == "Mini golden cross de 4 h · MC-V1_A (estrategia propia)" and a["probadas"] == 3
    assert a["robustez"]["corregida"]["valor"] == 0.06 and a["robustez"]["sin_5_mayores"] == -1.0
    assert "salida A" in a["regla"] and a["op_dia"] == 0.65 and a["pct_dia"] == 0.016
    f = filas["curso:b_filtro:MC-V1"]
    assert "prueba del filtro" in f["robustez"]["corregida"]["metodo"] and f["fuera"]["p"] == 0.3
    assert filas["curso:b_espera:MC-V1"]["robustez"]["mitades"] == [0.1, -0.1]


def test_rejillas_de_salidas_con_sus_reglas_y_sin_repetir_la_actual_ni_la_a():
    def comb(i, grupo, fam, sl, R, reglas, **kw):
        fu = {"n": 300, "op_dia": 1.0, "op_dia_natural": 0.7, "R_medio_funding": R, "gana_pct": 40,
              "cuenta_dia_jornada_pct": R * 0.5, "cuenta_dia_natural_pct": R * 0.35, "caida_max_R": 20.0}
        return {"id": i, "grupo": grupo, "fam": fam, "sl": sl, "dentro": dict(fu), "fuera": fu, "reglas": reglas, **kw}

    s11 = {"generado": 1_793_229_489_445, "dias_jornada": {"fuera": 306},
           "todas": [comb("actual", "actual", "F0", 1.5, -0.12, True), comb("A", "A", "F2", 2.0, -0.07, True),
                     comb("1:1 G1 stop 3 %", "G1", "F1", 3.0, -0.05, False)]}
    filas = ri.filas_salidas_1a1(s11)
    assert [f["id"] for f in filas] == ["salidas_1a1:1:1 G1 stop 3 %"] and filas[0]["cumple_reglas"] is False
    sm = {"generado": 1_793_213_020_110, "dias_jornada": {"fuera": 306},
          "todas": [comb("F0 stop 2 % · objetivo 4R · tope 7d", None, "F0", 2.0, 0.12, {"ganadoras_bajo_minimo": 0},
                         k=4.0, hs=168),
                    comb("F0 stop 1,5 % · objetivo 2R · tope 7d", None, "F0", 1.5, 0.01, {}, k=2.0, hs=168),
                    comb("F0 stop 1,5 % · objetivo 3R · tope 24h", None, "F0", 1.5, -0.12, {}, k=3.0, hs=24)]}
    por = {f["id"]: f for f in ri.filas_salidas_motor(sm)}
    f4 = por["salidas_motor:F0 stop 2 % · objetivo 4R · tope 7d"]
    assert f4["cumple_reglas"] is True and f4["pct_dia"] == 0.06 and f4["origen"] == "salidas" and f4["probadas"] == 3
    assert f4["caida_max_pct"] == ri.caida_aprox(20.0) and "aproximada" in f4["nota"]
    assert por["salidas_motor:F0 stop 1,5 % · objetivo 2R · tope 7d"]["cumple_reglas"] is False   # 2,76 % neto
    assert por["salidas_motor:F0 stop 1,5 % · objetivo 3R · tope 24h"]["igual_que"] == "seis"
    assert f4["nombre"].startswith("6 diarias · ") and "impulso de los 15 primeros min" in f4["regla"]
    # sin p: cada salida es una de las de la rejilla sobre las mismas entradas, así que no pasa la corrección
    assert f4["robustez"]["corregida"] == {"tipo": "sin_p", "valor": None, "pasa": False, "metodo": (
        "sin p ni corrección: una de 3 salidas de una rejilla sobre las mismas entradas de «6 diarias»")}
    n = ranking.normalizar(f4)
    assert n["fragil"] and n["fragil_motivos"] == ["sin corrección por número de pruebas: una de 3 configuraciones de su "
                                                   "estudio, que sube en el ranking por su resultado fuera de muestra"]


def test_anchos_solo_el_final_sin_las_referencias_ni_lo_de_salidas_motor():
    def caso(n, R, p):
        return {"n": n, "R_medio": R, "gana_pct": 30.0, "op_por_jornada": n / 306, "cuenta_por_jornada_pct": R * 0.5 * n / 306,
                "caida_max_cuenta_pct": 9.0, "R_sin_5_mayores": R - 0.2, "p": p}

    def comb(cid, entrada, stop, salida, R, p):
        return {"id": cid, "entrada": entrada, "entrada_nombre": {"seis": "6 diarias", "ruptura": "Ruptura V3"}[entrada],
                "stop_pct": stop, "salida": salida, "salida_nombre": f"objetivo fijo {salida}, como mucho 7 días",
                "referencia": {"dentro": {"backtest": caso(100, -0.02, 0.6)},
                               "fuera": {"backtest": caso(101, R, p), "paper_hoy": caso(101, R + 0.02, p),
                                         "sin_costes": caso(101, R + 0.07, p)}},
                "sin_xperps": {"fuera": {"sin_xperps": caso(90, -0.4, 0.9)}}}

    d = {"prueba": False, "generado": "29-10-2026 18:05", "jornadas": {"fuera": 306},
         "combinaciones": [comb("6 diarias · stop 2 % · salida A", "seis", 2.0, "A", -0.05, 0.8),
                           comb("6 diarias · stop 2 % · objetivo 3R", "seis", 2.0, "3R", 0.08, 0.3),
                           comb("6 diarias · stop 3 % · objetivo 4R", "seis", 3.0, "4R", 0.01, 0.4),
                           comb("Ruptura V3 · stop 3 % · objetivo 4R", "ruptura", 3.0, "4R", 0.2, 0.05)]}
    assert ri.filas_anchos({**d, "prueba": True}) == []
    ya_fila = {"id": "salidas_motor:F0 stop 2 % · objetivo 3R · tope 7d"}
    filas = {f["id"]: f for f in ri.filas_anchos(d, {(2.0, 3.0): ya_fila})}
    assert set(filas) == {"anchos:6 diarias · stop 3 % · objetivo 4R", "anchos:Ruptura V3 · stop 3 % · objetivo 4R"}
    assert set(ya_fila["perfiles"]) == {"sin_costes", "okx_eu_con_xperps", "okx_eu_sin_xperps"}   # lo suma a la de salidas_motor
    r = filas["anchos:Ruptura V3 · stop 3 % · objetivo 4R"]
    assert r["fuera"]["R"] == 0.2 and r["robustez"]["sin_5_mayores"] == 0.0 and r["fecha"] == "2026-10-29"
    assert r["robustez"]["corregida"]["valor"] == 0.2 and r["perfiles"]["okx_eu_con_xperps"]["R"] == 0.22   # Holm 4 × 0,05
    assert r["regla"].startswith("Entradas de Ruptura V3; stop 3,0 %") and r["perfil_costes"] == "backtest"


def test_escribir_es_idempotente_y_quita_lo_suyo_que_ya_no_sale(tmp_path):
    dest = tmp_path / "ranking"
    filas = {"selectiva": ri.filas_selectiva(SELECTIVA)[0], "seis2": [ri.filas_motor("seis2", motor("v2", RS, -0.07))]}
    assert ri.escribir(filas, dest, seco=True) == {"archivos": 4, "escritos": 4, "iguales": 0, "borrados": 0}
    assert not dest.exists()   # en seco no escribe nada
    assert ri.escribir(filas, dest)["escritos"] == 4
    nombres = sorted(p.name for p in dest.iterdir())
    assert "_importado.json" in nombres and all(n.startswith(("selectiva--", "seis2--", "_")) for n in nombres)
    man = (dest / "_importado.json").stat().st_mtime_ns
    assert ri.escribir(filas, dest) == {"archivos": 4, "escritos": 0, "iguales": 4, "borrados": 0}
    assert (dest / "_importado.json").stat().st_mtime_ns == man   # nada cambió: ni el resumen se reescribe
    (dest / "selectiva--vieja-00000000.json").write_text("{}", encoding="utf-8")   # de una importación anterior
    (dest / "curso--de-otra-00000000.json").write_text("{}", encoding="utf-8")     # de un estudio que no se leyó
    (dest / "mi_estudio.json").write_text(json.dumps({"id": "mio"}), encoding="utf-8")   # de otro: no se toca
    assert ri.escribir(filas, dest)["borrados"] == 1
    assert (dest / "curso--de-otra-00000000.json").exists() and (dest / "mi_estudio.json").exists()
    assert ri.escribir(filas, dest, limpiar=True)["borrados"] == 1 and (dest / "mi_estudio.json").exists()
    leidas = [json.loads(p.read_text(encoding="utf-8")) for p in dest.glob("selectiva--*.json")]
    assert all(ranking.normalizar(x) for x in leidas) and all(x["formato"] == 1 for x in leidas)


def test_main_de_principio_a_fin_con_un_backtest_pequeno(tmp_path, capsys):
    bt = tmp_path / "backtest"
    (bt / "controles" / "selectiva").mkdir(parents=True)
    (bt / "controles" / "selectiva" / "medir_selectiva.json").write_text(json.dumps(SELECTIVA), encoding="utf-8")
    (bt / "ruptura3_resumen.json").write_text(json.dumps(motor("Ruptura V3", RS, 0.4285, 0.386)), encoding="utf-8")
    (bt / "seis2_resumen.json").write_text(json.dumps(motor("6 diarias v2", RS, -0.0743)), encoding="utf-8")
    assert ri.main(["--origen", str(bt)]) == 0
    out = capsys.readouterr().out
    assert "En el ranking (no pierden fuera de muestra): 3 de 5" in out
    assert "1. Ruptura V3 [estudio] · +0,083 %/jornada" in out and "2. «Murphy»" in out
    assert "medir_anchos.json: aún no está" in out and "laboratorio.json: no está" in out
    r = ranking.Ranking(carpeta=bt / "ranking", lab_dir=tmp_path / "lab", estado_path=tmp_path / "e.json",
                        activa_path=tmp_path / "a.json")
    res = r.consulta({"openings": {"params": {"preset": "ruptura3"}, "estrategia_cargada": {}}}, todas=True)
    assert res["n_total"] == 5 and res["en_marcha"]["id"] == "ruptura3" and res["en_marcha"]["en_ranking"]
    assert ri.main(["--origen", str(bt)]) == 0 and "(0 escritas, 5 iguales, 0 quitadas)" in capsys.readouterr().out


def test_el_ejemplo_de_la_web_se_rehace_con_las_filas_importadas(tmp_path):
    """--demo: las filas reales del bloque rankingData salen de las importadas, normalizadas como las enseña el bot; las
    inventadas («Ejemplo: …») se quedan; el resto de la página no se toca; idempotente."""
    dest = tmp_path / "ranking"
    filas = {"ruptura3": [ri.filas_motor("ruptura3", motor("Ruptura V3", RS, 0.4285, op_jornada=0.386))],
             "impulso_7d": [ri.filas_motor("e2_impulso_7d", motor("E2 7d", RS, 0.375), e2_q=1.0)]}
    ri.escribir(filas, dest)
    viejo = {"formato": 1, "ejemplo": True, "tiempos": "relativos", "ts": -60000, "nota": "Ejemplo",
             "filas": [{"id": "ruptura3", "nombre": "Ruptura V3 vieja", "fuera": {"n": 1, "R": 9.9}},
                       {"id": "ej", "nombre": "Ejemplo: inventada </script>", "origen": "curso", "perfil_costes": "backtest",
                        "fuera": {"n": 50, "R": 0.2, "jornadas": 306}, "dentro": {"n": 60, "R": -0.1}}]}
    page = tmp_path / "radar.html"
    pre, post = '<html><script type="application/json" id="otroData">{"a":1}</script>\n', "\n<p>fin</p></html>"
    page.write_text(pre + '<script type="application/json" id="rankingData">' + json.dumps(viejo).replace("</", "<\\/")
                    + "</script>" + post, encoding="utf-8")
    res = ri.incrustar_demo(page, dest)
    assert res["ok"] and res["reales"] == 2 and res["ejemplos"] == 1 and res["cambia"] is True
    assert set(res["faltan"]) == {"seis2", "seis", f"lab2000:{ri.LAB_REF}", f"lab2000:{ri.LAB_E1}"}
    txt = page.read_text(encoding="utf-8")
    assert txt.startswith(pre) and txt.endswith(post) and txt.count("</script>") == 2   # el «</script>» del texto, escapado
    d = json.loads(ri.DEMO_BLOQUE.search(txt).group(2))
    por = {f["id"]: f for f in d["filas"]}
    assert set(por) == {"ej", "ruptura3", "e2_impulso_7d"} and por["ruptura3"]["nombre"] == "Ruptura V3"
    assert por["ruptura3"]["fuera"]["R"] == 0.4285 and por["e2_impulso_7d"]["fragil"] is True
    assert por["ej"]["fragil_motivos"] == ["gana fuera de muestra pero pierde dentro"] and "posicion" not in por["ej"]
    assert por["ej"]["nombre"] == "Ejemplo: inventada </script>" and d["criterio"] == ranking.CRITERIO
    assert d["perfiles_costes"] == [{"id": "backtest", "texto": ranking.PERFILES_TEXTO["backtest"], "n_ranking": 3}]
    assert d["ts"] == -60000 and d["tiempos"] == "relativos"   # lo demás del bloque, como estaba
    assert ri.incrustar_demo(page, dest)["cambia"] is False and page.read_text(encoding="utf-8") == txt
    assert ri.incrustar_demo(tmp_path / "radar.html", dest, seco=True)["cambia"] is False
    otra = tmp_path / "sin.html"
    otra.write_text("<html></html>", encoding="utf-8")
    assert ri.incrustar_demo(otra, dest) == {"ok": False, "why": "sin bloque rankingData"}


def test_utilidades():
    assert ri.fecha(1_793_034_145_937) == "2026-10-26" and ri.fecha("2026-10-29T02:43:41+01:00") == "2026-10-29"
    assert ri.fecha("29-10-2026 11:23") == "2026-10-29" and ri.fecha(None) is None
    assert ri.por_operacion([[0, 1.0], [1, 0.5], [2, 2.5]]) == [1.0, -0.5, 2.0]
    assert ri.sin_5([1, 2, 3]) is None and ri.mitades([1, 2]) is None and ri.caida_cuenta([]) is None
    assert ri.caida_cuenta([-1, -1]) == round((1 - 0.995 ** 2) * 100, 2) and ri.caida_aprox(None) is None
    assert ri.slug("lab2000:a|b,c") == ri.slug("lab2000:a|b,c") and ri.slug("x") != ri.slug("y")
    assert len(ri.archivo_de({"id": "z" * 500, "estudio": "curso"})) < 120
