"""Las salidas del laboratorio vivo (docs/07, 13) con el flujo entero en el proceso (admisión → rápida → completa →
robustez sobre una caché sintética, cada trabajo con etapas.ejecutar como haría su subproceso): la forma de
estado.json, el diario, la tarjeta, para_el_generador.json, ya_probado.json, las propuestas y la lectura de
decisiones\\; los textos en español y los números como números. Y la prueba en avance con su tabla."""
import json

import numpy as np
import pytest

from bot.lab_vivo import ajustes, almacen, catalogo, datos, etapas, salida_web
from bot.lab_vivo.corredor import Corredor
from test_lab_vivo_comun import DAY


class Hecho:
    def __init__(self, code):
        self.code = code

    def poll(self):
        return self.code

    def kill(self):
        pass


def lanzador_en_proceso(tid):
    tr = json.loads((ajustes.TRABAJOS / f"{tid}.trabajo.json").read_text(encoding="utf-8"))
    try:
        res = etapas.ejecutar(tr)
    except etapas.Pausado:
        return {"popen": Hecho(ajustes.CODIGO_PAUSADO), "pid": None}
    (ajustes.TRABAJOS / f"{tid}.resultado.json").write_text(json.dumps(etapas._redondea(res), default=str),
                                                             encoding="utf-8")
    return {"popen": Hecho(0), "pid": None}


@pytest.fixture(scope="module")
def flujo(tmp_path_factory):
    """El flujo entero una vez por módulo (tarda ≈ 10 s), en su propia carpeta temporal."""
    import os
    from bot import config
    viejo_dir, viejo_reglas = os.environ.get("BOT_LAB_VIVO_DIR"), config.USER_RULES
    os.environ["BOT_LAB_VIVO_DIR"] = str(tmp_path_factory.mktemp("flujo") / "lab_vivo")
    config.USER_RULES = True
    ajustes.recargar()
    ajustes.preparar_carpetas()
    try:
        yield _flujo()
    finally:
        config.USER_RULES = viejo_reglas
        if viejo_dir is None:
            os.environ.pop("BOT_LAB_VIVO_DIR", None)
        else:
            os.environ["BOT_LAB_VIVO_DIR"] = viejo_dir
        ajustes.recargar()


@pytest.fixture(autouse=True)
def _reglas(monkeypatch):
    from bot import config
    monkeypatch.setattr(config, "USER_RULES", True)


def _flujo():
    now = [ajustes.ahora_ms()]
    c = Corredor(procesos=1, sin_pausas=True, reloj=lambda: now[0], lanzador=lanzador_en_proceso,
                 memoria=lambda: 8000.0, cpu=lambda: 5.0, openings_on=lambda d: [])
    assert c.arrancar()
    ep = c.epoca()
    t0 = (now[0] // DAY - 230) * DAY
    per = {"epoca": ep, "inicio": t0 + 40 * DAY, "fin": t0 + 225 * DAY, "corte": t0 + 140 * DAY,
           "desde": datos.dia_mad(t0 + 40 * DAY), "hasta": datos.dia_mad(t0 + 224 * DAY),
           "corte_dia": datos.dia_mad(t0 + 140 * DAY)}
    fu = datos.fuente_sintetica(dias=230, semilla=31, t0=t0, periodo=per)
    datos.guardar_fuente(fu, ajustes.CACHE / f"{ep}-sint")
    with c.con:
        almacen.kv_set(c.con, "canario", {"estado": "ok", "firma_codigo": c.firma_cod, "ts": now[0],
                                          "libro": {"dentro_n": 2389, "dentro_R": -0.0569, "fuera_n": 1464,
                                                    "fuera_R": -0.075, "reproduce": True}})
    h = catalogo.siembra()[1]
    h["variantes"] = h["variantes"][:3]
    catalogo.escribir([h])
    for _ in range(40):
        c.paso()
        if not c.con.execute("SELECT 1 FROM trabajos WHERE estado IN ('pendiente', 'en_curso')").fetchone() \
                and not c.procs and not list(ajustes.TRABAJOS.glob("*.resultado.json")) or \
                c.con.execute("SELECT estado FROM hipotesis").fetchone()[0] in ("terminada", "rechazada", "error"):
            if c.con.execute("SELECT estado FROM hipotesis").fetchone()[0] in ("terminada", "rechazada", "error"):
                break
    c.consolidar()
    return c, h


def _numeros(x):
    """Ningún número va como texto en los JSON (los textos llevan la coma decimal)."""
    if isinstance(x, dict):
        for k, v in x.items():
            _numeros(v)
    elif isinstance(x, list):
        for v in x:
            _numeros(v)
    elif isinstance(x, str):
        try:
            float(x)
            raise AssertionError(f"número como texto: {x!r}")
        except ValueError:
            pass


def test_flujo_y_estado(flujo):
    c, h = flujo
    est = c.con.execute("SELECT estado FROM hipotesis").fetchone()[0]
    assert est == "terminada"
    assert almacen.n_total(c.con) == 3                        # cada variante cuenta una vez en N
    e = json.loads(ajustes.ESTADO.read_text(encoding="utf-8"))
    for k in ("formato", "ts", "latido", "pid", "estado", "aviso", "recursos", "datos", "codigo", "criterios",
              "contadores", "embudo", "meta", "en_curso", "cola", "mejores", "en_avance", "listas", "consolidacion",
              "diario_hoy", "errores"):
        assert k in e, k
    assert e["formato"] == 1 and "nada de esto opera" in e["aviso"]
    assert e["contadores"]["n"]["previo"] == 3917 and e["contadores"]["n"]["total"] == 3
    assert [x["etapa"] for x in e["embudo"]] == ["recibidas", "admitidas", "pasan_rapida", "pasan_completa",
                                                  "candidatas", "en_avance", "listas"]
    assert e["meta"]["objetivo_R_dia"] == 2.0 and e["meta"]["riesgo_pct"] == 0.5
    assert "no se sube" in e["meta"]["nota"]
    assert e["codigo"]["canario"]["estado"] == "ok" and e["criterios"]["version"] == 1
    _numeros({k: v for k, v in e.items() if k not in ("version", "codigo", "criterios")})


def test_tarjeta_y_mejores(flujo):
    c, h = flujo
    vid = f"{h['id']}:V1"
    t = salida_web.tarjeta(c.con, vid)
    for k in ("id", "hipotesis", "titulo", "familia", "regla", "clase", "cumple_reglas", "fuera_de_reglas", "dentro",
              "meta", "motivos", "checks"):
        assert k in t
    assert t["familia"] == "ruptura_canal" and t["regla"].startswith("Ruptura del canal")
    assert isinstance(t["dentro"]["n"], int)
    m = json.loads((ajustes.DIR / "mejores.json").read_text(encoding="utf-8"))
    assert m["formato"] == 1 and isinstance(m["mejores"], list)
    for x in m["mejores"]:
        assert x["fuera"] is not None


def test_diario_y_hipotesis(flujo):
    c, h = flujo
    dia = ajustes.dia_mad(ajustes.ahora_ms())
    d = json.loads((ajustes.DIARIO / f"{dia}.json").read_text(encoding="utf-8"))
    assert d["dia"] == dia and d["hipotesis"]["recibidas"] == 1 and d["variantes"]["rapida"] == 3
    assert d["texto"][0].startswith("Hoy: ")
    idx = json.loads((ajustes.DIR / "hipotesis.json").read_text(encoding="utf-8"))
    assert idx["hipotesis"][0]["id"] == h["id"]
    det = json.loads((ajustes.HIPOTESIS_DIR / f"{h['id']}.json").read_text(encoding="utf-8"))
    assert len(det["variantes"]) == 3 and det["variantes"][0]["pruebas"]


def test_para_el_generador_y_ya_probado(flujo):
    c, h = flujo
    g = json.loads((ajustes.DIR / "para_el_generador.json").read_text(encoding="utf-8"))
    assert g["reglas_usuario"]["stop_min_pct"] == 1.5 and g["reglas_usuario"]["riesgo_pct"] == 0.5
    assert g["cupo"]["pasada"] == 6 and len(g["familias"]) >= 25
    assert set(g["salidas"]) == {"A", "H", "C", "OBJ", "INFO"}
    assert any("E2" in x for x in g["lo_que_no"])
    ya = json.loads((ajustes.DIR / "ya_probado.json").read_text(encoding="utf-8"))
    assert len(ya["familias"]["ruptura_canal"]) == 3


def test_propuesta_y_decision(flujo, monkeypatch):
    c, h = flujo
    vid = f"{h['id']}:V2"
    real = c.estadistica_global

    def pasa(extra=None):   # la variante sigue pasando B7 y B8 con el N de hoy (si no, se revoca: otra prueba)
        st = real(extra)
        st["q"][vid], st["dsr"][vid] = 0.01, 0.99
        return st
    monkeypatch.setattr(c, "estadistica_global", pasa)
    with c.con:
        almacen.actualizar_variante(c.con, vid, clase="lista", avance_desde=ajustes.ahora_ms() - 30 * DAY)
    c.consolidar()
    props = list(ajustes.PROPUESTAS.glob("*.json"))
    assert len(props) == 1
    p = json.loads(props[0].read_text(encoding="utf-8"))
    assert p["tipo"] == "estrategia_lab_vivo" and p["estado"] == "pendiente" and p["variante"] == vid
    assert p["como_operarla"]["motor"] is False and "nada se activa sin ti" in p["aviso"]
    assert c.con.execute("SELECT clase FROM variantes WHERE id = ?", (vid,)).fetchone()[0] == "propuesta"
    (ajustes.DECISIONES / f"{p['id']}.json").write_text(json.dumps(
        {"formato": 1, "propuesta": p["id"], "decision": "descartada", "ts": 0, "por": "usuario", "nota": "no"}),
        encoding="utf-8")
    c.leer_decisiones()
    assert c.con.execute("SELECT clase FROM variantes WHERE id = ?", (vid,)).fetchone()[0] == "descartada_usuario"
    assert json.loads(props[0].read_text(encoding="utf-8"))["estado"] == "descartada"


def test_prueba_en_avance(flujo):
    c, h = flujo
    vid = f"{h['id']}:V1"
    car = datos.cache_existente(c.epoca())
    fu = datos.abrir_fuente(car)
    desde = fu.periodo["fin"] - 40 * DAY
    with c.con:
        almacen.actualizar_variante(c.con, vid, clase="en_avance", avance_desde=desde)
    v = almacen.fila_dict(c.con.execute("SELECT * FROM variantes WHERE id = ?", (vid,)).fetchone())
    tr = {"id": "avance-prueba", "tipo": "avance", "cache": str(car), "ahora": fu.periodo["fin"],
          "variantes": [c._payload_variante(v, c._hipotesis(h["id"]))]}
    res = etapas.ejecutar(tr)
    filas = res["variantes"][vid]
    assert filas and all(f["id"].startswith(vid + ":") for f in filas)
    assert any(f["previa"] for f in filas) or all(f["t_entrada"] >= desde for f in filas)
    with c.con:
        c._ingerir_avance(tr, res)
        c._ingerir_avance(tr, res)                         # un reinicio no duplica (id = variante:moneda:t_entrada)
    n = c.con.execute("SELECT COUNT(*) FROM avance WHERE variante = ?", (vid,)).fetchone()[0]
    assert n == len(filas)
    av = salida_web.avance_resumen(c.con, vid, desde)
    assert av["completas"] + av["abiertas"] == sum(1 for f in filas if not f["previa"])
    assert "operaciones" in av["faltan"] or "días" in av["faltan"] or av["faltan"].startswith("nada")


def test_revocacion_con_el_n_de_hoy(flujo):
    c, h = flujo
    vid = f"{h['id']}:V3"
    with c.con:
        almacen.actualizar_variante(c.con, vid, clase="en_avance", avance_desde=ajustes.ahora_ms())
    c.consolidar()
    assert c.con.execute("SELECT clase FROM variantes WHERE id = ?", (vid,)).fetchone()[0] == "revocada"
    assert c.con.execute("SELECT COUNT(*) FROM eventos WHERE tipo = 'revocada'").fetchone()[0] == 1


# ---------------------------------------------------------------- propuestas: revocadas, la última y la excepción del tiempo

def _variante_nueva(c, h, corta: str) -> str:
    """Una variante más de la hipótesis del flujo, lista para proponer (su propia firma: no toca las de las demás)."""
    hid = h["id"]
    v1 = c._variante(f"{hid}:V1")
    vid = f"{hid}:{corta}"
    with c.con:
        c.con.execute("INSERT OR REPLACE INTO variantes(id, hipotesis, vid, firma, params, salida, cumple_reglas, clase, "
                      "etapa, motivos, actualizada) VALUES (?, ?, ?, ?, ?, ?, 1, 'lista', 'avance', '[]', 0)",
                      (vid, hid, corta, f"firma-prueba-{corta}", almacen.dumps(v1["params"]), almacen.dumps(v1["salida"])))
        almacen.actualizar_variante(c.con, vid, avance_desde=ajustes.ahora_ms() - 30 * DAY)
    return vid


def _pasa(c, monkeypatch, vid):
    real = Corredor.estadistica_global.__get__(c)

    def pasa(extra=None):
        st = real(extra)
        st["q"][vid], st["dsr"][vid] = 0.01, 0.99
        return st
    monkeypatch.setattr(c, "estadistica_global", pasa)
    return real


def _propuestas(vid):
    return salida_web.propuestas_de(vid)


def _decidir(pid, decision, **extra):
    (ajustes.DECISIONES / f"{pid}.json").write_text(json.dumps(
        {"formato": 1, "propuesta": pid, "decision": decision, "ts": ajustes.ahora_ms(), "por": "usuario", **extra}),
        encoding="utf-8")


def _clase(c, vid):
    return c.con.execute("SELECT clase FROM variantes WHERE id = ?", (vid,)).fetchone()[0]


def test_propuesta_revocada_se_retira_y_no_se_puede_aceptar(flujo, monkeypatch):
    c, h = flujo
    vid = _variante_nueva(c, h, "V8")
    real = _pasa(c, monkeypatch, vid)
    c.consolidar()
    (p,) = _propuestas(vid)
    assert p["estado"] == "pendiente" and _clase(c, vid) == "propuesta"
    assert p["reglas"]["excepcion_tiempo"] and p["requiere"] == ["acepta_excepcion_tiempo"]
    assert "6 diarias" in p["reglas"]["excepcion_tiempo"]["texto"]
    _decidir(p["id"], "aceptada")                         # sin aceptar la excepción del cierre por tiempo
    c.leer_decisiones()
    p2 = salida_web.leer(ajustes.PROPUESTAS / f"{p['id']}.json")
    assert p2["estado"] == "pendiente" and "excepción del cierre por tiempo" in p2["falta"]
    assert _clase(c, vid) == "propuesta"
    n_ev = c.con.execute("SELECT COUNT(*) FROM eventos WHERE tipo = 'decision_incompleta'").fetchone()[0]
    c.leer_decisiones()
    assert c.con.execute("SELECT COUNT(*) FROM eventos WHERE tipo = 'decision_incompleta'").fetchone()[0] == n_ev
    monkeypatch.setattr(c, "estadistica_global", real)    # con el N de hoy deja de pasar B7 y B8
    c.consolidar()
    assert _clase(c, vid) == "revocada"
    p3 = salida_web.leer(ajustes.PROPUESTAS / f"{p['id']}.json")
    assert p3["estado"] == "retirada" and p3["motivo_retirada"].startswith("revocada")
    _decidir(p["id"], "aceptada", acepta_excepcion_tiempo=True)
    c.leer_decisiones()                                   # retirada: aceptarla no hace nada
    assert _clase(c, vid) == "revocada"


def test_solo_vale_la_ultima_propuesta_y_con_la_excepcion(flujo, monkeypatch):
    c, h = flujo
    vid = _variante_nueva(c, h, "V7")
    _pasa(c, monkeypatch, vid)
    c.consolidar()
    (p1,) = _propuestas(vid)
    real_ahora = ajustes.ahora_ms
    monkeypatch.setattr(ajustes, "ahora_ms", lambda: real_ahora() + 5_000)
    with c.con:
        almacen.actualizar_variante(c.con, vid, clase="lista")   # se vuelve a proponer: la anterior se retira
    c.consolidar()
    p1b, p2 = _propuestas(vid)
    assert p1b["id"] == p1["id"] and p1b["estado"] == "retirada" and "sustituida" in p1b["motivo_retirada"]
    assert p2["estado"] == "pendiente" and p2["id"] != p1["id"]
    _decidir(p1["id"], "aceptada", acepta_excepcion_tiempo=True)
    c.leer_decisiones()
    assert _clase(c, vid) == "propuesta"                  # la vieja no vale
    _decidir(p2["id"], "aceptada", acepta_excepcion_tiempo=True)
    c.leer_decisiones()
    assert _clase(c, vid) == "aceptada"
    fin = salida_web.leer(ajustes.PROPUESTAS / f"{p2['id']}.json")
    assert fin["estado"] == "aceptada" and fin["excepcion_tiempo_aceptada"] is True and "falta" not in fin


def test_el_generador_no_ve_el_fuera_de_muestra(flujo):
    c, h = flujo
    g = json.loads((ajustes.DIR / "para_el_generador.json").read_text(encoding="utf-8"))
    assert "estado.json" in g["no_leer"] and "mejores.json" in g["no_leer"] and "hipotesis\\" in g["no_leer"]
    ya = json.loads((ajustes.DIR / "ya_probado.json").read_text(encoding="utf-8"))
    vs = [v for lista in ya["familias"].values() for v in lista]
    assert vs and all("etapa" in v and "motivo" in v for v in vs)
    for x in [v["motivo"] for v in vs] + [d["motivo"] for d in g["descartes_recientes"]]:
        if x and x.split(":")[0] in salida_web.TEXTO_CODIGO:
            assert x == f"{x.split(':')[0]}: {salida_web.TEXTO_CODIGO[x.split(':')[0]]}"   # sin cifras del fuera
    assert any(v["dentro"] and isinstance(v["dentro"]["n"], int) for v in vs)   # las cifras de dentro, sí
    assert salida_web.motivo_para_generador("K2: R medio fuera -0.0123 (tiene que ser > 0)") == \
        "K2: R medio fuera de muestra ≤ 0"
    assert salida_web.motivo_para_generador("R2: R medio dentro -0.05 (tiene que ser > 0)").startswith("R2: R medio dentro")
    e = json.loads((ajustes.DIR / "para_el_generador_estado.json").read_text(encoding="utf-8"))
    assert set(e) >= {"estado", "latido", "cupo"} and "mejores" not in e


def test_avance_sin_operaciones_fantasma(flujo):
    c, h = flujo
    vid = f"{h['id']}:V9"
    fila = {"variante": vid, "moneda": "BTC", "lado": "largo", "t_senal": 1, "R": 0.5, "R_f": 0.5, "pct": 0.25,
            "motivo": "objetivo", "previa": 0, "cuerpo": {}}
    a = dict(fila, id=f"{vid}:BTC:100", t_entrada=100, t_salida=200, completa=1)
    b = dict(fila, id=f"{vid}:BTC:300", t_entrada=300, t_salida=None, completa=0, motivo="fin de datos")
    tr = {"id": "avance-fantasma"}
    t = ajustes.ahora_ms() + DAY
    with c.con:
        c._ingerir_avance(tr, {"variantes": {vid: [a, b]}, "ahora": t})
    with c.con:   # al recalcular, la abierta ya no sale (un hueco que se rellenó): se quita, no se queda de fantasma
        c._ingerir_avance(tr, {"variantes": {vid: [a]}, "ahora": t + 10_000})
    ids = [r[0] for r in c.con.execute("SELECT id FROM avance WHERE variante = ?", (vid,))]
    assert ids == [a["id"]]
    ev = c.con.execute("SELECT texto FROM eventos WHERE tipo = 'avance_recalculo' AND sujeto = ?", (vid,)).fetchall()
    assert ev and "1 operaciones del avance ya no salen" in ev[-1][0]
    with c.con:   # un resultado más viejo que el último ingerido no cambia nada
        c._ingerir_avance(tr, {"variantes": {vid: [a, b]}, "ahora": t + 5_000})
    assert c.con.execute("SELECT COUNT(*) FROM avance WHERE variante = ?", (vid,)).fetchone()[0] == 1


def test_robustez_directa_e_ingesta(flujo):
    """La etapa de robustez de una variante (B1 a B6 y lo informado), con su ingesta: B7 y B8 con el N acumulado."""
    c, h = flujo
    vid = f"{h['id']}:V1"
    car = datos.cache_existente(c.epoca())
    hh = c._hipotesis(h["id"])
    v1 = c._payload_variante(c._variante(vid), hh)
    v2 = c._payload_variante(c._variante(f"{h['id']}:V2"), hh)
    tr = {"id": "robustez-prueba", "tipo": "robustez", "objetivo": vid, "cache": str(car), "epoca": c.epoca(),
          "firma_codigo": c.firma_cod, "variante": v1, "hermanas": [v2], "fuera_conocidas": {}, "fuera_de_reglas": []}
    res = etapas.ejecutar(tr)
    assert res["B1"]["modo"] in ("dos_sentidos", "un_sentido") and 0 < res["B1"]["p"] <= 1
    assert set(res["estres"]) == {"desliz_x2_y_stops", "entrada_1_min_tarde", "sin_xperps", "objetivo_superado_2pb"}
    assert f"{h['id']}:V2" in res["hermanas"]
    assert all(m.split(":")[0] in ("B1", "B2", "B3", "B4", "B5", "B6") for m in res["motivos"])
    assert res["pasa"] == (not res["motivos"])
    with c.con:
        c._ingerir_robustez(tr, res)
    clase = c.con.execute("SELECT clase FROM variantes WHERE id = ?", (vid,)).fetchone()[0]
    p = almacen.fila_dict(c.con.execute("SELECT * FROM pruebas WHERE variante = ? AND etapa = 'robustez'",
                                        (vid,)).fetchone())
    assert p["cuenta_en_n"] == 0 and "n_dsr" in p["metricas"]
    if clase == "descartada_robustez":
        assert p["motivos"]
    else:
        assert clase in ("en_avance", "candidata_hermana")
