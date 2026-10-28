"""Evolución de la estrategia (bot/evolucion.py), sin red: historial que no se borra, reglas del usuario, campeón frente
a su gemelo virtual y los aspirantes con datos nuevos y completos (promoción con Holm y confirmación, pausa con
evidencia, mínimos), versión del motor por firma, bloqueo de lo descartado o revertido, ajustes solo como propuesta,
modo automático, aprendizaje y fuentes enlazados, informe semanal, mensaje «evolucion» y fallos que no paran nada."""
import asyncio
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bot import config
from bot import evolucion as E
from bot.bus import Bus
from bot.store import Store

MAD = ZoneInfo("Europe/Madrid")
H, DAY = E.H, E.DAY


def ms(y, m, d, h=0, mi=0) -> int:
    return int(datetime(y, m, d, h, mi, tzinfo=MAD).timestamp() * 1000)


DESDE = ms(2026, 10, 28)                 # medianoche de Madrid: desde aquí cuenta la vigilancia
T1 = DESDE + 57 * DAY                    # primera evaluación: el aspirante cumple y queda en observación
AHORA = DESDE + 77 * DAY                 # segunda: lo confirma con datos posteriores que no se solapan
LAB_REF = E._preset()[1]                 # entrada del preset IMPULSO con salida «sig» (la hipótesis congelada)
LAB7 = E.gemelo_preset()                 # la misma con 7 días de seguridad: el gemelo del campeón (en_paper)
ASP = "ap_mom:15:0,2|rvol:1,5|todos|a_favor|todas|todas|todos|t:30|2,5|3|7d"        # stop 2,5 % y 3R: cumple
MALA = "ap_mom:15:0,2|rvol:1,5|todos|a_favor|todas|todas|todos|t:30|1,5|2|7d"       # 3 % bruto: 2,76 % neto
OTRA = "cad_ult:0|ninguna|todos|ninguno|todas|todas|todos|t:30|3|3|sig"              # familia que el motor no opera
OTRA7 = OTRA.rsplit("|", 1)[0] + "|7d"
VIG_SQL = """CREATE TABLE IF NOT EXISTS vigilancia(id TEXT PRIMARY KEY, estrategia TEXT, apertura TEXT, moneda TEXT,
  lado TEXT, entrada_t INTEGER, entrada REAL, stop REAL, objetivo REAL, salida_t INTEGER, salida REAL, R REAL, pct REAL,
  motivo TEXT, cuerpo TEXT)"""
TRADES_SQL = "CREATE TABLE IF NOT EXISTS trades(id TEXT PRIMARY KEY, story TEXT, opened INTEGER, closed INTEGER, body TEXT)"
COINS = ("BTC", "ETH", "SOL", "XRP")


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "bot.db")
    store.db.execute(VIG_SQL)
    store.db.execute(TRADES_SQL)
    store.db.commit()
    bus = Bus(store)
    yield store, bus, tmp_path / "estrategia_activa.json"
    store.close()


def evo(env, **kw) -> E.Evolucion:
    store, bus, path = env
    kw.setdefault("clock", lambda: AHORA)
    kw.setdefault("n_boot", 1000)
    return E.Evolucion(bus, store, archivo=path, **kw)


def meta(bus, items):
    """items: (id, código) o (id, código, campos extra como en_paper y variante_de)."""
    bus.publish({"type": "vigilancia", "estrategias": [
        {"id": x[0], "codigo": x[1], "nombre": f"Estrategia {x[1]}", "clase": "prometedora", "origen": "congelada",
         "desde": DESDE, **(x[2] if len(x) > 2 else {})} for x in items]})


def virtuales(store, sid, cod, n, win_mod, wins=(0, 1, 2), start=DESDE, previa=False, R_win=2.9, R_loss=-1.02):
    """n operaciones virtuales cerradas (una al día, apertura de Nueva York): gana si i % win_mod está en wins."""
    for i in range(n):
        t = start + i * DAY + 15 * H
        R = R_win if i % win_mod in wins else R_loss
        day = datetime.fromtimestamp(t / 1000, MAD).date().isoformat()
        store.db.execute("INSERT OR REPLACE INTO vigilancia VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (f"{cod}|nuevayork-{day}|{COINS[i % 4]}{'|p' if previa else ''}", sid, f"nuevayork-{day}",
                          COINS[i % 4], "largo", t, 100.0, 97.0, 109.0, t + 5 * H, 100 + R, R, R * 2.5,
                          "objetivo" if R > 0 else "stop",
                          json.dumps({"ventana": "nuevayork", "previa": previa, "apertura_ms": t})))
    store.db.commit()


def trade(store, i, t, R, window="nuevayork", coin="BTC", noticia="a_favor", vol=50.0, firma=None, abierta=False):
    market = {"nuevayork": "nuevayork", "europa": "londres"}[window]
    tags = {"noticia": noticia, "volatilidad_pct": vol, "apertura_anterior": "a_favor", "btc": "a_favor"}
    if firma:
        tags["estrategia_firma"] = firma
    body = {"id": f"t{i}", "story": f"a-{window}-2026-11-01-{coin.lower()}-{i}",
            "strategy": f"impulso-largo-{'noticia-a-favor' if noticia == 'a_favor' else 'sin-noticia'}",
            "window": f"Apertura {E.WIN_NAME[window]}", "market": market, "coin": coin, "side": "long",
            "opened": t, "tags": tags}
    if not abierta:
        body.update(closed=t + 2 * H, R=R, pnl=R * 50)
    store.db.execute("INSERT INTO trades VALUES (?,?,?,?,?)", (body["id"], body["story"], t,
                                                               None if abierta else t + 2 * H, json.dumps(body)))


def escenario_promocion(store, bus, n=70):
    """Gemelo del campeón flojo (30 % de aciertos) y aspirante fuerte (60 %), además de uno que rompe las reglas, otro
    que el motor no sabe operar y la hipótesis congelada con salida «sig». Más calentamiento que no cuenta."""
    meta(bus, [(LAB_REF, "E1"), (LAB7, "E1-7d", {"en_paper": True, "variante_de": LAB_REF}), (ASP, "E2"),
               (MALA, "E3"), (OTRA, "E4")])
    virtuales(store, LAB_REF, "E1", n, 5, (0, 1, 2))
    virtuales(store, LAB7, "E1-7d", n, 10, (0, 3, 6))
    virtuales(store, ASP, "E2", n, 5, (0, 1, 2))
    virtuales(store, MALA, "E3", n, 5, (0, 1, 2, 3))
    virtuales(store, OTRA, "E4", n, 5, (0, 1, 2, 3))
    virtuales(store, ASP, "E2", 8, 1, (0,), start=DESDE - 10 * DAY, previa=True, R_win=-5.0)   # no cuenta


async def promover(ev, t1=T1, t2=AHORA):
    """Las dos evaluaciones que hacen falta: la primera deja al aspirante en observación, la segunda lo confirma."""
    await ev.evaluar(t1)
    return await ev.evaluar(t2)


# ---------------------------------------------------------------- historial

def test_registrar_y_leer_con_filtros_y_nada_se_borra(env):
    store, _, _ = env
    a = E.registrar(store, "fuente_silenciada", "ruido.net", {"estado": "activa"}, {"estado": "silenciada"},
                    "Acierta 4 de 40 (10 %)", {"casos": 40, "aciertos": 4}, "fuentes_vivas", ts=1000)
    b = E.registrar(store, "peso_cambiado", "coindesk.com", 1.0, 1.3, "Acierta 30 de 45", {"casos": 45}, "aprendizaje",
                    ts=2000)
    E.registrar(store, "peso_cambiado", "theblock.co", {"peso": 1.0}, {"peso": 0.8}, "Baja", None, "aprendizaje", ts=3000)
    assert a["etiqueta"] == "Medio silenciado" and a["id"].startswith("v1000-")
    todos = E.leer(store)
    assert [e["sujeto"] for e in todos] == ["theblock.co", "coindesk.com", "ruido.net"]   # del más reciente al más viejo
    assert E.leer(store, tipo="peso_cambiado", limite=1)[0]["sujeto"] == "theblock.co"
    assert len(E.leer(store, tipo="fuente_silenciada,peso_cambiado")) == 3
    assert [e["id"] for e in E.leer(store, desde=1500, hasta=2500)] == [b["id"]]
    assert E.leer(store, origen="fuentes_vivas")[0]["evidencia"] == {"casos": 40, "aciertos": 4}
    assert E.leer(store, sujeto="coindesk.com")[0]["antes"] == 1.0
    with pytest.raises(sqlite3.DatabaseError):
        store.db.execute("DELETE FROM evolution_log")
    with pytest.raises(sqlite3.DatabaseError):
        store.db.execute("UPDATE evolution_log SET motivo='x'")
    store.db.rollback()
    assert len(E.leer(store)) == 3


def test_cambio_del_filtro_de_noticias_se_apunta_una_sola_vez(env):
    """Revisión del 28-10-2026 (hallazgo n.º 4): el cambio del filtro de noticias entra con un reinicio y cambia lo que
    filtra la estrategia en paper; queda en el historial (no reversible desde la web) una sola vez, aunque se reinicie."""
    store, _, _ = env
    ev = evo(env)
    e, f = ev.apuntar_cambios_codigo()
    assert e["id"] == "codigo-noticias-2026-10-28" and e["tipo"] == "filtro_cambiado" and e["origen"] == "codigo"
    assert e["etiqueta"] == "Filtro de noticias cambiado" and "dos regímenes" in e["motivo"]
    assert "nueva_cartera_paper.py" in e["motivo"] and e["evidencia"]["azar_confirmada_pct"]["umbral_2sigma"] == 5.0
    # la entrada por noticia y los topes compartidos (revisión de código del 28-10-2026): también una sola vez
    assert f["id"] == "codigo-entrada-noticias-2026-10-28" and f["origen"] == "codigo" and "mezcla" in f["motivo"].lower()
    assert "noticia_fiable" in f["despues"]["entrada_por_noticia"] and f["evidencia"]["pasan_la_puerta"] == 0
    assert ev.apuntar_cambios_codigo() == [] and E.apuntar_cambios_codigo(store) == []   # reinicio: no se repite
    assert evo(env).apuntar_cambios_codigo() == []
    assert sorted(x["id"] for x in E.leer(store, tipo="filtro_cambiado")) == [
        "codigo-entrada-noticias-2026-10-28", "codigo-noticias-2026-10-28"]
    assert "filtro_cambiado" in E.NO_REVERSIBLES and "filtro_cambiado" not in E.PROPIOS


def test_las_entradas_por_noticia_no_cuentan_como_del_campeon():
    """Con el preset, las operaciones sin firma cuentan como de E2; las de la entrada por noticia fiable no (comparten
    la cartera, pero son otra estrategia)."""
    ahora = DESDE + 30 * DAY

    def tr(i, origen=None):
        tags = {"origen": origen} if origen else {}
        return {"opened": DESDE + i * DAY, "closed": DESDE + i * DAY + H, "R": 1.0, "pnl": 1.0, "coin": "BTC",
                "side": "long", "tags": tags, "strategy": "x", "story": f"s{i}"}

    paper = [E.op_paper(tr(i)) for i in range(4)] + [E.op_paper(tr(10 + i, "noticia_fiable")) for i in range(3)]
    assert [o["origen"] for o in paper] == [None] * 4 + ["noticia_fiable"] * 3
    res = E.calcular(paper, [], [], E.activa_por_defecto(), ahora, n_boot=50)
    assert res["campeon"]["vivo"]["operaciones"] == 4


def test_registrar_desde_otra_conexion_tipos_nuevos_y_errores(env, tmp_path):
    store, _, _ = env
    other = sqlite3.connect(store.path)   # como lo haría otro módulo desde un hilo
    E.registrar(other, "medio_experimental", "x.com", None, None, "Prueba de un tipo nuevo", origen="otro")
    other.close()
    e = E.leer(store)[0]
    assert e["tipo"] == "medio_experimental" and e["etiqueta"] == "Medio experimental"
    with pytest.raises(ValueError):
        E.registrar(store, "", "x", motivo="sin tipo")
    assert E.registrar(store, "peso_cambiado", "y.com", 1, 2)["motivo"] == "(sin motivo)"
    vacia = sqlite3.connect(tmp_path / "vacia.db")
    assert E.leer(vacia) == []   # sin tabla todavía
    vacia.close()


# ---------------------------------------------------------------- reglas, estrategia activa y versión del motor

def test_reglas_del_usuario_siempre_aunque_esten_apagadas(monkeypatch):
    monkeypatch.setattr(config, "USER_RULES", False)
    assert E.cumple_reglas({}) == (True, [])                                 # preset IMPULSO: 3 % y 3R
    assert not E.cumple_reglas({"sl_pct": 1.0})[0]
    assert not E.cumple_reglas({"target_r": 1.5})[0]
    ok, fallos = E.cumple_reglas({"sl_pct": 1.5, "target_r": 2.0})           # 3 % bruto: 2,76 % neto
    assert not ok and "neto" in fallos[0] and "2,76" in fallos[0]
    assert E.cumple_reglas({"sl_pct": 1.5, "target_r": 2.5})[0]              # 3,51 % neto
    for bad in ({"be_r": 1.0}, {"trail_atr": 6.0}, {"tp1_frac": 0.5}, {"min_net_pct": 2.0}, {"target_pct": 3.0},
                {"estrategia": "ruptura"}):
        assert not E.cumple_reglas(bad)[0], bad


def test_lab_a_params_solo_con_la_salida_del_motor():
    p, why = E.lab_a_params(LAB7)
    assert why is None and p["measure_min"] == 15 and p["min_move_pct"] == 0.2 and p["entry"] == "t:30"
    assert p["rvol_min"] == 1.5 and p["btc_filter"] == "a_favor" and p["sl_pct"] == 3.0 and p["target_r"] == 3.0
    assert p["windows"] == list(E.WIN_IDS) and p["coins"] == list(config.COINS) and p["max_hold_h"] == 168
    p, _ = E.lab_a_params("ap_mom:30:0,5|ninguna|no_baja|ninguno|europa|SOL+XRP|todos|pb:30|2|3|7d",
                          {"news_mode": "a_favor"})
    assert p["entry"] == "pb" and p["rvol_min"] is None and p["windows"] == ["europa"] and p["coins"] == ["SOL", "XRP"]
    assert p["max_hold_h"] == 168 and p["news_mode"] == "a_favor" and p["vol_filter"] == "no_baja"
    # Lo medido con otra salida no es lo que se aplicaría: ni «sig» ni 24 h (que además añadiría un tope de tiempo)
    for sid in (LAB_REF, ASP.rsplit("|", 1)[0] + "|24h"):
        p, why = E.lab_a_params(sid)
        assert p is None and "salida" in why and "7 días" in why
    assert E.lab_a_params(OTRA)[0] is None and "familia" in E.lab_a_params(OTRA)[1]
    assert E.lab_a_params(ASP.replace("rvol:1,5", "vwap"))[0] is None
    assert E.lab_a_params(ASP.replace("|todos|t:30", "|mar-jue|t:30"))[0] is None
    assert E.lab_a_params("basura")[0] is None


def test_gemelo_del_preset_y_firma_del_motor(tmp_path, monkeypatch):
    from bot import apertura, vigilancia
    # Sin estrategia_activa.json: la prueba no depende de lo que opere la cartera paper real (DATA_DIR del PC)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    assert LAB7 == vigilancia.paper_id() and LAB7.endswith("|7d") and LAB_REF.endswith("|sig")
    d = E.activa_por_defecto()
    assert d["lab_id"] == LAB_REF and d["lab_twin"] == LAB7
    pre = E._preset()[0]
    f = E.firma_estrategia(d)
    # La versión que calcula el motor con sus parámetros completos es la misma que la del preset
    assert f == E.firma_motor(apertura.impulse_params({})[0]) == E.firma_motor(pre)
    assert E.firma_motor({**pre, "max_hold_h": 168.0, "windows": list(reversed(pre["windows"]))}) == f
    assert E.firma_motor({**pre, "sl_pct": 2.5}) != f
    assert E.firma_motor({**pre, "pausada": True}) == f                      # la pausa no es otra versión


def test_estrategia_activa_archivo(tmp_path):
    path = tmp_path / "estrategia_activa.json"
    assert E.leer_estrategia_activa(path) is None                            # sin archivo: el preset
    path.write_text("{roto", encoding="utf-8")
    assert E.leer_estrategia_activa(path) is None
    d = {"version": 1, "estrategia": "impulso", "params": {"sl_pct": 1.0, "target_r": 3.0}}
    path.write_text(json.dumps(d), encoding="utf-8")
    assert E.leer_estrategia_activa(path) is None                            # rompe las reglas: no se carga
    d["params"] = {"sl_pct": 2.5, "target_r": 3.0}
    E.escribir_activa(path, d)
    assert E.leer_estrategia_activa(path) == d
    assert E.params_motor(d) == {"estrategia": "impulso", "sl_pct": 2.5, "target_r": 3.0}
    assert E.params_motor(E.activa_por_defecto()) is None
    assert not (tmp_path / "estrategia_activa.json.tmp").exists()


# ---------------------------------------------------------------- estadística

def _ops(Rs, start=DESDE):
    return [{"t": start + i * DAY, "t_out": start + i * DAY + H, "R": R, "gana": R > 0} for i, R in enumerate(Rs)]


def test_bootstrap_y_contraccion():
    a = _ops([2.9 if i % 5 < 3 else -1.0 for i in range(50)])
    b = _ops([2.9 if i % 10 in (0, 3, 6) else -1.0 for i in range(50)])
    r = E.bootstrap_mejora(a, b, n_boot=1000, semilla=1)
    assert r["dif"] > 1 and r["p"] < 0.01 and r["p_menor"] > 0.9 and r["ic95"][0] > 0
    assert r["p"] == max(r["p_boot"], r["p_t"])                               # el más prudente de las dos pruebas
    assert r == E.bootstrap_mejora(a, b, n_boot=1000, semilla=1)              # reproducible
    same = E.bootstrap_mejora(b, b, n_boot=1000)
    assert same["dif"] == 0 and same["p"] > 0.4
    assert E.bootstrap_mejora([], b)["p"] is None and E.bootstrap_mejora(a, [])["dif"] is None
    pierde = E.bootstrap_mejora(_ops([-0.8] * 30), None)
    assert pierde["p_menor"] < 0.01 and pierde["ic95"][1] < 0
    few = E.resumen(_ops([2.0] * 5), DESDE + 5 * DAY)
    assert few["R_medio"] == 2.0 and 0 < few["R_contraido"] < 0.5 and few["muestra"].startswith("muy poca")
    old = E.contraido(_ops([2.0] * 40, DESDE - 365 * DAY), DESDE)[0]
    new = E.contraido(_ops([2.0] * 40), DESDE + 40 * DAY)[0]
    assert old < new                                                          # lo antiguo pesa menos


def test_t_de_student_y_errores_robustos():
    for t, gl in ((12.706, 1), (4.303, 2), (3.182, 3), (2.571, 5), (2.306, 8), (2.042, 30), (1.984, 100)):
        assert abs(E.t_cola(t, gl) - 0.025) < 2e-4 and abs(E.t_cola(-t, gl) - 0.975) < 2e-4
    assert E.t_cola(0.0, 7) == 0.5
    tr = E.t_robusta(_ops([1.0, -1.0] * 20), None)                            # 40 días: 6 bloques de 7 días
    assert tr["gl"] == 5 and tr["p"] == 0.5
    assert E.t_robusta(_ops([1.0] * 3), None)["gl"] is None                   # un solo bloque: sin p
    assert E.t_robusta(_ops([-0.8] * 30), None)["p_menor"] == 1.0 - E.t_robusta(_ops([-0.8] * 30), None)["p"]


def test_solo_cuentan_operaciones_de_horizonte_completo():
    ahora = DESDE + 30 * DAY
    ops = (_ops([1.0] * 10)                                                   # viejas y cerradas: cuentan
           + [{"t": ahora - 2 * DAY, "t_out": ahora - DAY, "R": -1.0, "gana": False}]   # reciente ya cerrada (stop)
           + [{"t": ahora - DAY, "t_out": None, "R": None, "gana": None}]               # reciente abierta
           + [{"t": DESDE + 12 * DAY, "t_out": None, "R": None, "gana": None}])         # vieja aún abierta: rara
    cuentan, en_curso, sin_res = E.completas(ops, ahora, 168)
    assert len(cuentan) == 10 and en_curso == 2 and sin_res == 1               # el stop reciente no sesga nada
    assert len(E.completas(ops, ahora, 24)[0]) == 11


def test_holm_entre_aspirantes():
    def asp(p, dif=0.5, compatible=True):
        return {"compatible": compatible, "reglas_ok": compatible,
                "frente": {"p": p, "dif_R": dif, "faltan": [], "pruebas": None, "p_max_corregido": None}}

    xs = [asp(0.03), asp(0.2), asp(0.5), asp(0.001, compatible=False)]
    assert E._holm(xs) == 3
    assert not xs[0]["frente"]["cumple"] and "con 3 aspirantes comparables hace falta < 0,0167" in \
        xs[0]["frente"]["faltan"][0]                                          # p = 0,03 no basta con 3 pruebas
    assert xs[0]["frente"]["p_max_corregido"] == round(0.05 / 3, 5) and xs[3]["frente"]["pruebas"] is None
    ys = [asp(0.02), asp(0.01), asp(0.5)]
    E._holm(ys)
    assert ys[1]["frente"]["cumple"] and ys[0]["frente"]["cumple"] and not ys[2]["frente"]["cumple"]
    assert ys[0]["frente"]["p_max_corregido"] == 0.025                        # el segundo: 0,05 / 2


def test_curva_semanal_sin_huecos():
    ops = [{"t": DESDE, "t_out": DESDE + H, "R": 1.0, "gana": True},
           {"t": DESDE + 15 * DAY, "t_out": DESDE + 15 * DAY, "R": -1.0, "gana": False}]
    c = E.curva_semanal(ops, DESDE, DESDE + 20 * DAY)
    assert [x["operaciones"] for x in c] == [1, 0, 1, 0]
    assert [x["R_acum"] for x in c] == [1.0, 1.0, 0.0, 0.0] and c[0]["semana"] == "2026-W44"


def test_semanas():
    lunes = ms(2026, 11, 2)
    assert E.semana_objetivo(lunes + 8 * H) == "2026-W44"                    # el lunes a las 8: la semana pasada
    assert E.semana_objetivo(lunes + 6 * H) == "2026-W43"                    # a las 6 aún no
    assert E.semana_limites("2026-W44") == (ms(2026, 10, 26), lunes)
    assert E._texto_semana("2026-W44") == "del 26 de octubre al 1 de noviembre de 2026"
    assert E.proxima_evaluacion(lunes + 8 * H) == ms(2026, 11, 9, 7)


# ---------------------------------------------------------------- campeón y aspirantes

async def test_promocion_observacion_confirmacion_y_aplicada(env):
    store, bus, path = env
    escenario_promocion(store, bus)
    ev = evo(env)
    pan = await ev.evaluar(T1)
    asp = {a["codigo"]: a for a in pan["aspirantes"]}
    assert set(asp) == {"E1", "E2", "E3", "E4"}                               # E1-7d es el gemelo del campeón
    assert pan["campeon"]["gemelo"]["id"] == LAB7 and pan["campeon"]["virtual"]["operaciones"] == 50
    fr = asp["E2"]["frente"]
    assert fr["referencia"] == "virtual" and fr["operaciones"] == 50 and fr["cumple"] and fr["p"] < 0.05
    assert fr["pruebas"] == 1 and fr["p_max_corregido"] == 0.05               # solo E2 es comparable
    assert fr["confirmacion"]["estado"] == "en_observacion" and not pan["candidatas"]   # una mirada no basta
    assert asp["E2"]["vivo"]["operaciones"] == 50 and asp["E2"]["en_curso"] == 20     # el calentamiento no cuenta
    assert not asp["E1"]["compatible"] and "salida" in asp["E1"]["por_que_no"] and "(E1-7d)" in asp["E1"]["por_que_no"]
    assert not asp["E3"]["frente"]["cumple"] and any("reglas" in f for f in asp["E3"]["frente"]["faltan"])
    assert not asp["E4"]["frente"]["cumple"] and any("no sabe operarla" in f for f in asp["E4"]["frente"]["faltan"])
    pan = await ev.evaluar(AHORA)                                             # 13 operaciones nuevas de cada lado
    fr = next(a for a in pan["aspirantes"] if a["codigo"] == "E2")["frente"]
    assert fr["confirmacion"]["estado"] == "confirmada" and fr["confirmacion"]["operaciones_nuevas"] == 13
    props = (await ev.api_propuestas("pendiente"))["propuestas"]
    assert [p["tipo"] for p in props] == ["estrategia_promovida"]
    assert not path.exists()                                                  # modo proponer: nada se aplica solo
    msg = bus.state["evolucion"]
    json.dumps(msg)
    assert msg["modo"] == "proponer" and msg["propuestas"][0]["titulo"] == "Sustituir el campeón por E2"
    assert msg["estado"] == "ok" and msg["campeon"]["lab_id"] == LAB_REF and msg["reglas"]["solo_paper"]
    assert "Holm" in msg["propuestas"][0]["texto"] and msg["propuestas"][0]["evidencia"]["pruebas"] == 1
    assert not msg["campeon"]["pendiente_reinicio"]                           # preset sin dato del motor: se supone
    # Otra evaluación no duplica la propuesta: refresca sus cifras
    await ev.evaluar(AHORA + H)
    assert len((await ev.api_propuestas())["propuestas"]) == 1
    res = await ev.aplicar(props[0]["id"])
    assert res["ok"], res
    act = E.leer_estrategia_activa(path)
    assert act["lab_id"] == ASP and act["lab_twin"] == ASP and act["params"]["sl_pct"] == 2.5
    assert act["desde"] == AHORA and not act["pausada"] and act["firma"] == E.firma_estrategia(act)
    assert act["params"]["max_hold_h"] == 168 and E.params_motor(act)["estrategia"] == "impulso"
    e = E.leer(store, tipo="estrategia_promovida")[0]
    assert e["antes"]["fuente"] == "preset" and e["despues"]["lab_id"] == ASP and e["id"] == act["registro"]
    assert e["evidencia"]["operaciones"] == 70 and e["evidencia"]["p"] < 0.05 and e["evidencia"]["por"] == "usuario"
    assert e["evidencia"]["firma_despues"] == act["firma"] != e["evidencia"]["firma_antes"]
    assert (await ev.api_propuestas("aplicada"))["propuestas"][0]["id"] == props[0]["id"]
    assert (await ev.aplicar(props[0]["id"]))["ok"] is False                  # ya aplicada
    # Ahora el campeón es E2 (su gemelo es él mismo en la vigilancia) y E1-7d pasa a aspirante (no le gana)
    pan = await ev.evaluar(AHORA + 2 * H)
    assert pan["campeon"]["gemelo"]["id"] == ASP and pan["campeon"]["pendiente_reinicio"]
    assert {a["codigo"] for a in pan["aspirantes"]} == {"E1", "E1-7d", "E3", "E4"}
    assert not pan["candidatas"]
    # Reversible: vuelve el preset, la propuesta queda revertida y bloqueada, y se reinicia el enfriamiento
    ev.clock = lambda: AHORA + DAY
    res = await ev.revertir(e["id"])
    assert res["ok"], res
    assert E.leer_estrategia_activa(path)["fuente"] == "preset"
    hist = E.leer(store)
    assert hist[0]["revierte"] == e["id"] and hist[0]["tipo"] == "estrategia_degradada"
    assert next(x for x in hist if x["id"] == e["id"])["revertida_por"] == hist[0]["id"]
    assert (await ev.revertir(e["id"]))["ok"] is False                        # ya revertido
    assert (await ev.api_propuestas("revertida"))["propuestas"][0]["id"] == props[0]["id"]
    assert ev._estado("ultimo_cambio") == AHORA + DAY
    assert bus.state["evolucion"]["bloqueos"][0]["motivo"] == "revertida"


async def test_gemelo_real_sin_promocion_vacia(env):
    """Con los ids reales: la hipótesis congelada (LAB_REF, «sig») y su variante de 7 días marcada en_paper, que es
    la réplica exacta del motor. Esa es el gemelo, nunca un aspirante, y la «sig» no se puede aplicar."""
    store, bus, path = env
    meta(bus, [(OTRA, "E1"), (OTRA7, "E1-7d", {"variante_de": OTRA}), (LAB_REF, "E2"),
               (LAB7, "E2-7d", {"en_paper": True, "variante_de": LAB_REF})])
    virtuales(store, OTRA, "E1", 70, 5, (0, 1, 2))
    virtuales(store, OTRA7, "E1-7d", 70, 5, (0, 1, 2))
    virtuales(store, LAB_REF, "E2", 70, 10, (0, 3, 6))   # «sig» floja
    virtuales(store, LAB7, "E2-7d", 70, 5, (0, 1, 2))    # 7 días mejor (como en el laboratorio)
    ev = evo(env)
    pan = await promover(ev)
    assert pan["campeon"]["gemelo"]["id"] == LAB7 and pan["campeon"]["gemelo"]["codigo"] == "E2-7d"
    assert {a["codigo"] for a in pan["aspirantes"]} == {"E1", "E1-7d", "E2"}
    e2 = next(a for a in pan["aspirantes"] if a["codigo"] == "E2")
    assert not e2["compatible"] and "salida" in e2["por_que_no"] and "(E2-7d)" in e2["por_que_no"]
    assert pan["pruebas"] == 0 and not pan["candidatas"] and not path.exists()


async def test_archivo_sin_gemelo_lo_busca_por_parametros(env):
    """Un archivo (antiguo) con el preset escrito a mano y sin lab_twin: la estrategia de la vigilancia con sus mismos
    parámetros efectivos es su gemelo, no un aspirante (promoverla no cambiaría nada)."""
    store, bus, path = env
    p, _ = E.lab_a_params(LAB7, E._preset()[0])
    E.escribir_activa(path, {"version": 1, "estrategia": "impulso", "fuente": "evolucion", "params": p,
                             "lab_id": LAB_REF, "nombre": "A mano", "pausada": False, "solo_paper": True})
    meta(bus, [(LAB7, "E1-7d"), (ASP, "E2")])
    virtuales(store, LAB7, "E1-7d", 70, 5, (0, 1, 2, 3))
    virtuales(store, ASP, "E2", 70, 10, (0, 3, 6))
    pan = await promover(evo(env))
    assert pan["campeon"]["gemelo"]["id"] == LAB7
    assert [a["codigo"] for a in pan["aspirantes"]] == ["E2"] and not pan["candidatas"]


async def test_sin_muestra_suficiente_no_hay_propuesta(env):
    store, bus, path = env
    escenario_promocion(store, bus, n=39)
    pan = await promover(evo(env))
    fr = next(a for a in pan["aspirantes"] if a["codigo"] == "E2")["frente"]
    assert not fr["cumple"] and any("hacen falta 40" in f for f in fr["faltan"])
    assert not pan["candidatas"] and not path.exists()


async def test_nunca_se_compara_con_cero_si_falta_el_gemelo(env):
    """Gemelo con 19 operaciones (+0,94 R) y aspirante con 60 (+0,55 R): sin referencia suficiente no hay promoción;
    tampoco sin gemelo."""
    store, bus, _ = env
    meta(bus, [(LAB7, "E1-7d", {"en_paper": True}), (ASP, "E2")])
    virtuales(store, LAB7, "E1-7d", 19, 2, (0,))
    virtuales(store, ASP, "E2", 60, 5, (0, 1))
    ev = evo(env)
    pan = await promover(ev)
    fr = next(a for a in pan["aspirantes"] if a["codigo"] == "E2")["frente"]
    assert fr["operaciones"] == 60 and not fr["cumple"] and fr["p"] is None
    assert any("gemelo del campeón lleva 19" in f for f in fr["faltan"]) and not pan["candidatas"]
    meta(bus, [(ASP, "E2")])                                                  # ahora sin gemelo
    pan = await ev.evaluar(AHORA + H)
    fr = pan["aspirantes"][0]["frente"]
    assert fr["referencia"] is None and any("no tiene gemelo" in f for f in fr["faltan"]) and not pan["candidatas"]


async def test_paper_del_campeon_como_segunda_comprobacion(env):
    store, bus, _ = env
    escenario_promocion(store, bus)
    for i in range(45):   # el campeón en paper le gana al aspirante: no hay promoción
        trade(store, i, DESDE + i * DAY + 15 * H + 1, 2.5 if i % 4 else -1.0, coin=COINS[i % 4])
    store.db.commit()
    pan = await promover(evo(env))
    fr = next(a for a in pan["aspirantes"] if a["codigo"] == "E2")["frente"]
    assert fr["referencia"] == "virtual" and fr["paper_operaciones"] == 45 and not fr["cumple"]
    assert any("no supera al campeón en paper" in f for f in fr["faltan"])
    assert pan["campeon"]["vivo"]["operaciones"] == 45 and not pan["candidatas"]
    br = pan["campeon"]["brecha_paper_virtual"]
    assert br["paper_operaciones"] == 45 and br["dif_R"] > 1 and "Brecha paper–virtual" in br["texto"]


async def test_caducada_si_los_datos_dejan_de_respaldarla(env):
    store, bus, _ = env
    escenario_promocion(store, bus)
    ev = evo(env)
    await promover(ev)
    pid = (await ev.api_propuestas("pendiente"))["propuestas"][0]["id"]
    store.db.execute("UPDATE vigilancia SET R = -1.0 WHERE estrategia = ?", (ASP,))
    store.db.commit()
    res = await ev.aplicar(pid)                                               # se recalcula al aplicar
    assert not res["ok"] and "ya no la respaldan" in res["why"]
    assert (await ev.api_propuestas("caducada"))["propuestas"][0]["id"] == pid
    # Caducar no bloquea: con los datos de vuelta se propone otra vez; y una evaluación también caduca
    virtuales(store, ASP, "E2", 70, 5, (0, 1, 2))
    await ev.evaluar(AHORA + H)
    pid2 = (await ev.api_propuestas("pendiente"))["propuestas"][0]["id"]
    store.db.execute("UPDATE vigilancia SET R = -1.0 WHERE estrategia = ?", (ASP,))
    store.db.commit()
    await ev.evaluar(AHORA + 2 * H)
    assert not (await ev.api_propuestas("pendiente"))["propuestas"]
    assert pid2 in {p["id"] for p in (await ev.api_propuestas("caducada"))["propuestas"]}


async def test_descartar_bloquea_y_solo_vuelve_con_datos_nuevos(env):
    store, bus, path = env
    escenario_promocion(store, bus, n=150)
    ev = evo(env)
    await promover(ev)
    pid = (await ev.api_propuestas("pendiente"))["propuestas"][0]["id"]
    assert (await ev.descartar(pid, "Prefiero esperar"))["ok"]
    p = (await ev.api_propuestas("descartada"))["propuestas"][0]
    assert p["nota"] == "Prefiero esperar" and p["resuelta_por"] == "usuario"
    with pytest.raises(sqlite3.DatabaseError):
        store.db.execute("DELETE FROM evolution_proposals")
    store.db.rollback()
    pan = await ev.evaluar(AHORA + 7 * DAY)                                   # no vuelve: los datos son los mismos
    assert not pan["candidatas"] and not (await ev.api_propuestas("pendiente"))["propuestas"]
    fr = next(a for a in pan["aspirantes"] if a["codigo"] == "E2")["frente"]
    assert fr["bloqueo_desde"] == AHORA and any("hacen falta 40" in f for f in fr["faltan"])
    # Con 43 operaciones posteriores a tu decisión cumple otra vez, y con 13 más se confirma: vuelve, marcada
    await ev.evaluar(AHORA + 50 * DAY)
    pan = await ev.evaluar(AHORA + 70 * DAY)
    props = (await ev.api_propuestas("pendiente"))["propuestas"]
    assert [x["tipo"] for x in props] == ["estrategia_promovida"] and props[0]["cuerpo"]["bloqueada"]["ts"] == AHORA
    assert props[0]["cuerpo"]["evidencia"]["desde"] >= AHORA and "Ya la descartaste" in props[0]["cuerpo"]["texto"]
    assert bus.state["evolucion"]["propuestas"][0]["bloqueada"] and not path.exists()


async def test_modo_automatico_aplica_con_enfriamiento(env):
    store, bus, path = env
    escenario_promocion(store, bus)
    ev = evo(env, modo="automatico")
    ev._set_estado("ultimo_cambio", AHORA - 3 * DAY)                          # hace 3 días hubo otro cambio
    await promover(ev)
    assert not path.exists() and len((await ev.api_propuestas("pendiente"))["propuestas"]) == 1
    assert bus.state["evolucion"]["propuestas"][0]["automatica"]
    ev._set_estado("ultimo_cambio", AHORA - 20 * DAY)
    await ev.evaluar(AHORA + H)
    act = E.leer_estrategia_activa(path)
    assert act["lab_id"] == ASP
    e = E.leer(store, tipo="estrategia_promovida")[0]
    assert e["evidencia"]["por"] == "automático" and e["evidencia"]["modo"] == "automatico"


async def test_automatico_no_rehace_lo_que_reviertes(env):
    store, bus, path = env
    escenario_promocion(store, bus, n=150)
    ev = evo(env, modo="automatico")
    await promover(ev)
    promo = E.leer(store, tipo="estrategia_promovida")
    assert len(promo) == 1 and E.leer_estrategia_activa(path)["lab_id"] == ASP
    ev.clock = lambda: AHORA + DAY
    assert (await ev.revertir(promo[0]["id"]))["ok"]                         # el usuario la revierte al día siguiente
    assert ev._estado("ultimo_cambio") == AHORA + DAY                         # reinicia el enfriamiento
    for dias in (15, 51, 71):                                                 # pasan el enfriamiento y los mínimos
        ev.clock = lambda d=dias: AHORA + d * DAY
        await ev.evaluar(AHORA + dias * DAY)
        assert E.leer_estrategia_activa(path)["fuente"] == "preset", dias
    assert len(E.leer(store, tipo="estrategia_promovida")) == 1               # nunca se volvió a aplicar sola
    props = (await ev.api_propuestas("pendiente"))["propuestas"]              # pero sí se vuelve a proponer
    assert len(props) == 1 and props[0]["cuerpo"]["bloqueada"]["motivo"] == "revertida"
    assert not bus.state["evolucion"]["propuestas"][0]["automatica"]


async def test_version_del_motor_y_pendiente_de_reinicio(env):
    """Las operaciones se atribuyen por la firma que el motor copia en sus etiquetas, no por la hora: tras un cambio,
    hasta que el motor confirma que cargó la versión nueva, nada de la vieja cuenta para ella ni se evalúa la pausa."""
    store, bus, path = env
    for i in range(30):                                                       # el preset, antes de la integración
        trade(store, i, DESDE + i * DAY + 15 * H, -0.8)
    store.db.commit()
    motor = [None]
    ev = evo(env, leer_motor=lambda: motor[0])
    pan = await ev.refrescar()
    assert pan["campeon"]["vivo"]["operaciones"] == 30 and pan["campeon"]["degradado"]   # sin etiqueta = preset
    p, _ = E.lab_a_params(ASP, E._preset()[0])
    nueva = {"version": 2, "estrategia": "impulso", "fuente": "evolucion", "params": p, "lab_id": ASP,
             "lab_twin": ASP, "nombre": "E2", "pausada": False, "desde": DESDE, "solo_paper": True}
    E.escribir_activa(path, nueva)
    f_new, f_old = E.firma_estrategia(nueva), E.firma_estrategia(E.activa_por_defecto())
    for i in range(30):                                                       # el motor sin reiniciar: firma vieja
        trade(store, 100 + i, DESDE + i * DAY + 16 * H, -0.8, firma=f_old)
    store.db.commit()
    motor[0] = {"firma": f_old, "ts": DESDE - DAY}
    pan = await ev.evaluar()
    c = pan["campeon"]
    assert c["firma"] == f_new and c["pendiente_reinicio"] and c["motor"]["texto"].startswith("Pendiente:")
    assert "≤ 1 min" in c["motor"]["texto"] and "no hace falta reiniciar" in c["motor"]["texto"]
    assert c["vivo"]["operaciones"] == 0 and not c["degradado"] and not pan["candidatas"]
    for i in range(30):                                                       # ya con la versión nueva
        trade(store, 200 + i, DESDE + i * DAY + 17 * H, -0.8, firma=f_new)
    store.db.commit()
    pan = await ev.evaluar(AHORA + H)
    assert pan["campeon"]["vivo"]["operaciones"] == 30 and not pan["campeon"]["degradado"]   # aún sin confirmar
    motor[0] = {"firma": f_new, "ts": AHORA}                                  # cargada en el último arranque
    pan = await ev.evaluar(AHORA + 2 * H)
    c = pan["campeon"]
    assert not c["pendiente_reinicio"] and c["desde"] == DESDE and c["degradado"]   # «desde»: cuando se aplicó
    assert [x["tipo"] for x in pan["candidatas"]] == ["estrategia_degradada"]


async def test_degradacion_pausa_y_reversion(env):
    store, bus, path = env
    for i in range(30):
        trade(store, i, DESDE + i * DAY + 15 * H, -0.8)
    store.db.commit()
    ev = evo(env)
    pan = await ev.evaluar()
    d = pan["campeon"]["degradacion"]
    assert pan["campeon"]["degradado"] and d["evidencia_clara"] and d["R_contraido"] < -0.3 and d["ic95"][1] < 0
    assert [c["tipo"] for c in pan["candidatas"]] == ["estrategia_degradada"]
    pid = (await ev.api_propuestas("pendiente"))["propuestas"][0]["id"]
    assert (await ev.aplicar(pid))["ok"]
    act = E.leer_estrategia_activa(path)
    assert act["pausada"] is True and E.params_motor(act) is None             # el motor mira «pausada» aparte
    pausa = E.leer(store, tipo="estrategia_degradada")[0]
    assert pausa["antes"]["pausada"] is False and pausa["evidencia"]["operaciones"] == 30
    pan = await ev.evaluar(AHORA + H)
    assert pan["campeon"]["pausada"] and not pan["candidatas"]               # pausado: no se vuelve a proponer
    assert "La pausa se aplica cuando el motor la cargue" in pan["campeon"]["motor"]["texto"]
    assert (await ev.revertir(pausa["id"]))["ok"]
    assert E.leer_estrategia_activa(path)["pausada"] is False
    assert E.leer(store)[0]["tipo"] == "estrategia_promovida" and E.leer(store)[0]["revierte"] == pausa["id"]
    pan = await ev.evaluar(AHORA + 2 * H)                                     # revertida: con los mismos datos, no
    assert not pan["candidatas"] and pan["campeon"]["degradacion"]["operaciones"] == 0


async def test_pausa_automatica_solo_si_se_repite(env):
    store, bus, path = env
    for i in range(30):
        trade(store, i, DESDE + i * DAY + 15 * H, -0.8)
    store.db.commit()
    ev = evo(env, modo="automatico")
    await ev.evaluar()
    props = (await ev.api_propuestas("pendiente"))["propuestas"]
    assert not path.exists() and props[0]["tipo"] == "estrategia_degradada"
    await ev.evaluar(AHORA + H)                                               # mirar otra vez enseguida no cuenta
    assert not path.exists()
    await ev.evaluar(AHORA + 7 * DAY)                                         # se repite la semana siguiente: ahora sí
    assert E.leer_estrategia_activa(path)["pausada"] is True


async def test_degradacion_exige_minimo_de_operaciones(env):
    store, bus, _ = env
    for i in range(29):
        trade(store, i, DESDE + i * DAY + 15 * H, -1.0)
    store.db.commit()
    pan = await evo(env).evaluar()
    assert not pan["campeon"]["degradado"] and not pan["candidatas"]


async def test_degradacion_exige_evidencia_clara(env):
    """R medio −0,7 en 30 operaciones pero con mucha dispersión (y contraído < −0,3): el IC llega a positivo, así que
    no se concluye nada."""
    store, bus, _ = env
    Rs = [3.3 if c == "+" else -4.7 for c in "++--++---+--+-++-+-+++-+-+--+-"]   # 15 y 15: media −0,7
    for i, R in enumerate(Rs):
        trade(store, i, DESDE + i * DAY + 15 * H, R)
    store.db.commit()
    pan = await evo(env, clock=lambda: DESDE + 37 * DAY).evaluar()
    d = pan["campeon"]["degradacion"]
    assert d["operaciones"] == 30 and d["R_contraido"] < -0.3 and not d["evidencia_clara"]
    assert not pan["campeon"]["degradado"] and not pan["candidatas"]


async def test_ajuste_de_parametros_solo_propuesta(env):
    store, bus, path = env
    k = 0
    for i in range(30):                                                       # Nueva York pierde siempre
        trade(store, k, DESDE + i * DAY + 15 * H, -1.0, "nuevayork", COINS[i % 4])
        k += 1
    for i in range(60):                                                       # Europa va bien
        trade(store, k, DESDE + (i // 2) * DAY + 9 * H + (i % 2) * H, 2.0 if i % 2 else -0.4, "europa",
              COINS[(i // 2) % 4])
        k += 1
    store.db.commit()
    ev = evo(env, modo="automatico")
    pan = await ev.evaluar()
    assert [c["titulo"] for c in pan["candidatas"]] == ["Quitar la apertura de Nueva York"]
    c = pan["candidatas"][0]
    assert c["evidencia"]["operaciones_grupo"] == 30 and c["evidencia"]["pruebas"] == 2
    assert "no tendrá gemelo" in c["texto"]                                   # aquí no hay vigilancia
    assert not path.exists()                                                  # ni en automático se aplica solo
    pid = (await ev.api_propuestas("pendiente"))["propuestas"][0]["id"]
    assert (await ev.aplicar(pid))["ok"]
    act = E.leer_estrategia_activa(path)
    assert act["params"]["windows"] == ["tokio-seul", "china", "europa", "saopaulo"] and act["lab_id"] is None
    assert act["desde"] == AHORA and act["firma"] != E.firma_estrategia(E.activa_por_defecto())
    e = E.leer(store, tipo="parametro_cambiado")[0]
    assert e["antes"]["params"] == {} and e["despues"]["params"]["windows"] == act["params"]["windows"]
    assert E.cumple_reglas(act["params"])[0]
    pan = await ev.evaluar(AHORA + H)                                         # las de antes son de otra versión
    assert pan["campeon"]["vivo"]["operaciones"] == 0 and not pan["candidatas"]
    assert pan["campeon"]["pendiente_reinicio"]


async def test_ajuste_con_vigilancia_mantiene_el_gemelo_filtrado(env):
    store, bus, path = env
    meta(bus, [(LAB7, "E1-7d", {"en_paper": True})])
    virtuales(store, LAB7, "E1-7d", 40, 5, (0, 1))
    for i in range(30):
        trade(store, i, DESDE + i * DAY + 15 * H, -1.0, "nuevayork", COINS[i % 4])
    for i in range(60):
        trade(store, 100 + i, DESDE + (i // 2) * DAY + 9 * H + (i % 2) * H, 2.0 if i % 2 else -0.4, "europa",
              COINS[(i // 2) % 4])
    store.db.commit()
    ev = evo(env)
    await ev.evaluar()
    pid = (await ev.api_propuestas("pendiente"))["propuestas"][0]["id"]
    assert (await ev.aplicar(pid))["ok"]
    act = E.leer_estrategia_activa(path)
    assert act["lab_twin"] == LAB7 and "nuevayork" not in act["twin_filtro"]["windows"]
    pan = await ev.refrescar(AHORA + H)
    g = pan["campeon"]["gemelo"]
    assert g["id"] == LAB7 and g["filtro"] and "aproximado" in g["texto"]
    assert pan["campeon"]["virtual"]["operaciones"] == 0                      # todas las virtuales eran de Nueva York


async def test_ajuste_no_se_propone_con_poca_muestra(env):
    store, bus, _ = env
    for i in range(20):
        trade(store, i, DESDE + i * DAY + 15 * H, -1.0, "nuevayork")
    for i in range(40):
        trade(store, 100 + i, DESDE + i * DAY + 9 * H, 1.5, "europa")
    store.db.commit()
    assert not (await evo(env).evaluar())["candidatas"]


async def test_abiertas_y_recientes_en_curso(env):
    store, bus, _ = env
    meta(bus, [(LAB7, "E1-7d", {"en_paper": True})])
    virtuales(store, LAB7, "E1-7d", 10, 2, (0,))
    t = AHORA - 2 * DAY                                                       # virtual abierta, reciente
    store.db.execute("INSERT INTO vigilancia VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("E1-7d|x|BTC", LAB7, "nuevayork-x", "BTC", "largo", t, 100.0, 97.0, 109.0, None, None, None,
                      None, "abierta", json.dumps({"ventana": "nuevayork"})))
    store.db.execute("INSERT INTO vigilancia VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("E1-7d|y|BTC", LAB7, "nuevayork-y", "BTC", "largo", t - 30 * DAY, 100.0, 97.0, 109.0, None, None,
                      None, None, "sin_datos", json.dumps({"ventana": "nuevayork"})))   # no cuenta en nada
    for i in range(5):
        trade(store, i, DESDE + i * DAY + 15 * H, 1.0)                        # completas
    trade(store, 10, AHORA - DAY, -1.0)                                       # reciente, ya cerrada en stop
    trade(store, 11, AHORA - DAY, None, abierta=True)                         # reciente, abierta
    trade(store, 12, AHORA - 30 * DAY, None, abierta=True)                    # vieja aún abierta: sin resolver
    store.db.commit()
    pan = await evo(env).refrescar()
    c = pan["campeon"]
    assert c["vivo"]["operaciones"] == 5 and c["en_curso"] == 2 and c["sin_resolver"] == 1
    assert c["virtual"]["operaciones"] == 10 and c["virtual_en_curso"] == 1


# ---------------------------------------------------------------- informe semanal y bucle

async def test_informe_semanal_una_vez_por_semana(env):
    store, bus, _ = env
    lunes = ms(2026, 11, 2)
    meta(bus, [(LAB_REF, "E1"), (ASP, "E2")])
    virtuales(store, ASP, "E2", 5, 5, (0, 1, 2))
    trade(store, 1, ms(2026, 10, 27, 15), 2.9, noticia="a_favor")
    trade(store, 2, ms(2026, 10, 28, 15), -1.02, noticia="neutral")
    trade(store, 3, ms(2026, 10, 29, 9), 2.9, "europa", "SOL")
    trade(store, 4, ms(2026, 10, 30, 15), -1.02, noticia="a_favor")
    store.db.commit()
    E.registrar(store, "fuente_silenciada", "ruido.net", {"estado": "activa"}, {"estado": "silenciada"},
                "Acierta 3 de 40", {"casos": 40}, "fuentes_vivas", ts=ms(2026, 10, 30, 12))
    E.registrar(store, "peso_cambiado", "theblock.co", {"peso": 1.0}, {"peso": 1.2}, "Acierta 20 de 30",
                {"casos": 30}, "aprendizaje", ts=ms(2026, 10, 31, 12))
    E.registrar(store, "peso_cambiado", "fuera.com", 1.0, 2.0, "Otra semana", ts=ms(2026, 11, 3))

    def medios():
        return [{"medio": "coindesk.com", "peso": 1.4, "peso_antes": 1.0, "casos": 30, "aciertos": 21},
                {"medio": "humo.io", "peso": 0.3, "peso_antes": 1.0}]

    now = [lunes + 8 * H]
    ev = evo(env, clock=lambda: now[0], leer_medios=medios)
    await ev.paso()
    infs = (await ev.api_informes())["informes"]
    assert len(infs) == 1
    inf = infs[0]
    assert inf["semana"] == "2026-W44" and inf["titulo"] == "Informe de la semana del 26 de octubre al 1 de noviembre de 2026"
    txt = inf["texto"]
    assert "Esta semana cerró 4 operaciones" in txt and "Con noticia a favor: 3 op." in txt
    assert "Medios silenciados (1): ruido.net" in txt
    assert "Suben: coindesk.com (1,00 → 1,40, 21 de 30 aciertos), theblock.co (1,00 → 1,20)" in txt
    assert "Bajan: humo.io (1,00 → 0,30)" in txt and "fuera.com" not in txt
    assert "Qué cambió y por qué" in txt and "Acierta 3 de 40" in txt and "E2" in txt
    assert [s["titulo"] for s in inf["secciones"]] == ["Resumen", "Qué ha funcionado", "Medios que suben y bajan",
                                                        "Qué estrategia lidera", "Qué cambió y por qué",
                                                        "Propuestas pendientes", "Avisos"]
    assert inf["cifras"]["semana"]["operaciones"] == 4
    assert bus.state["evolucion"]["informe"]["semana"] == "2026-W44"
    assert E.leer(store, tipo="informe_semanal")[0]["motivo"].startswith("Informe semanal automático")
    now[0] += 20 * 60_000
    await ev.paso()                                                           # misma semana: solo refresca
    assert len((await ev.api_informes())["informes"]) == 1
    assert (await ev.api_informes(semana="2026-W44"))["informes"][0]["id"] == inf["id"]
    h = await ev.api_historial(tipo="informe_semanal")
    assert h["cambios"][0]["despues"] == {"titulo": inf["titulo"], "resumen": inf["resumen"]}   # versión ligera


async def test_informe_con_fallo_de_medios_no_para(env):
    def medios():
        raise RuntimeError("sin aprendizaje")

    ev = evo(env, clock=lambda: ms(2026, 11, 2, 8), leer_medios=medios)
    e = await ev.informe_semanal()
    assert e is not None and "No pude leer el aprendizaje de medios" in e["despues"]["texto"]
    assert "Aún no hay operaciones suficientes" in e["despues"]["texto"]


async def test_fallos_no_paran_nada(env):
    store, bus, path = env

    def roto():
        raise RuntimeError("tabla rota")

    ev = evo(env, leer_vigilancia=roto)
    assert await ev.evaluar() is None
    assert bus.state["evolucion"]["estado"] == "error" and "tabla rota" in bus.state["evolucion"]["nota"]
    await ev.paso()                                                           # no lanza
    path.write_text("{roto", encoding="utf-8")
    ev2 = evo(env, leer_motor=roto)                                           # tampoco si falla leer el motor
    assert ev2.activa()["fuente"] == "preset" and "No pude leer" in ev2.nota_archivo
    await ev2.refrescar()
    assert bus.state["evolucion"]["campeon"]["nota"].startswith("No pude leer")
    assert bus.state["evolucion"]["estado"] == "sin_datos"


async def test_vigilancia_inyectable_sin_meta(env):
    store, bus, _ = env

    def leer():
        filas = [{"estrategia": ASP, "apertura": "europa-2026-11-01", "moneda": "BTC", "lado": "corto",
                  "entrada_t": DESDE + i * DAY, "salida_t": DESDE + i * DAY + H, "R": 1.0, "pct": 2.5,
                  "cuerpo": {"previa": False}} for i in range(3)]
        return {"estrategias": [], "operaciones": filas}

    pan = await evo(env, leer_vigilancia=leer).refrescar()
    a = pan["aspirantes"][0]
    assert a["id"] == ASP and a["vivo"]["operaciones"] == 3 and a["frente"]["referencia"] is None
    assert any("no tiene gemelo" in f for f in a["frente"]["faltan"])


# ---------------------------------------------------------------- órdenes de la web

async def test_ordenes_web(env):
    store, bus, _ = env
    ev = evo(env)
    assert ev.orden_web({"type": "evolucion", "accion": "borrar"}) is False
    assert ev.orden_web({"type": "decision", "story": "n1"}) is False
    assert ev.orden_web({"type": "evolucion", "accion": "aplicar", "id": 5}) is True
    await asyncio.gather(*ev._tasks)
    assert ev.ultima_orden["ok"] is False and "Falta el id" in ev.ultima_orden["why"]
    assert ev.orden_web({"type": "evolucion", "accion": "modo", "modo": "automatico"})
    await asyncio.gather(*ev._tasks)
    assert ev.modo == "automatico" and bus.state["evolucion"]["modo"] == "automatico"
    e = E.leer(store, sujeto="evolucion.modo")[0]
    assert e["antes"] == {"modo": "proponer"} and e["despues"] == {"modo": "automatico"}
    assert evo(env).modo == "automatico"                                      # se guarda
    assert (await ev.atender({"accion": "modo", "modo": "a_lo_loco"}))["ok"] is False
    assert (await ev.revertir(e["id"]))["ok"]
    assert ev.modo == "proponer"
    assert (await ev.atender({"accion": "evaluar"}))["ok"] is True
    assert bus.state["evolucion"]["ultima_orden"]["accion"] == "evaluar"
    assert (await ev.atender({"accion": "aplicar", "id": "p-no-existe"}))["why"] == "No existe esa propuesta"
    vista = await ev.api_vista()
    assert vista["type"] == "evolucion" and vista["criterios"]["promocion_min_operaciones"] == 40


async def test_revertir_cambios_de_otros_modulos(env):
    store, bus, _ = env
    llamadas = []

    async def reversor(e):
        llamadas.append(e["id"])
        E.registrar(store, "fuente_restaurada", e["sujeto"], e["despues"], e["antes"], "Restaurado desde la web",
                    origen="fuentes_vivas", revierte=e["id"])
        return {"ok": True, "why": None}

    sil = E.registrar(store, "fuente_silenciada", "x.com", {"estado": "activa"}, {"estado": "silenciada"}, "Mal",
                      origen="fuentes_vivas")
    peso = E.registrar(store, "peso_cambiado", "y.com", 1.0, 0.5, "Baja", origen="aprendizaje")
    ev = evo(env, reversores={"fuente_silenciada": reversor})
    assert (await ev.revertir(sil["id"]))["ok"] and llamadas == [sil["id"]]
    assert E.leer(store, id_=sil["id"])[0]["revertida_por"] is not None
    res = await ev.revertir(peso["id"])
    assert not res["ok"] and "aprendizaje" in res["why"]


@dataclass
class FilaFake:
    estado: str
    previo: str = "activa"


ESTADO_ACC = {"activa": "activar", "prueba": "a_prueba", "silenciada": "silenciar", "retirada": "retirar"}


class FakeFuentes:
    """Lo justo de FuentesVivas: filas {clave: Fila(estado, previo)}, accion() cambia el estado y llama a
    registrar(entrada) en el acto, con su formato (acciones e inversas exactas como fuentes_vivas._accion/_deshacer)."""

    def __init__(self):
        self.registrar, self.filas = None, {"medio:ruido.net": FilaFake("activa"), "medio:nuevo.io": FilaFake("prueba")}
        self.ts = AHORA

    def cambiar(self, clave, nuevo, motivo, auto=True):
        fila = self.filas[clave]
        antes = fila.estado
        baja = antes in ("activa", "prueba") and nuevo in ("silenciada", "retirada")
        if baja:
            fila.previo = antes
        fila.estado = nuevo
        accion = ({"silenciada": "silenciar", "retirada": "retirar"}.get(nuevo)
                  or ("restaurar" if antes in ("silenciada", "retirada") else ESTADO_ACC[nuevo]))
        inversa = "restaurar" if baja else ESTADO_ACC.get(antes, "retirar")
        self.ts += 1000
        self.registrar({"ts": self.ts, "area": "fuentes", "accion": accion, "id": clave, "nombre": clave[6:],
                        "antes": antes, "despues": nuevo, "tipo": "acierto" if auto else "manual", "motivo": motivo,
                        "cifras": {"casos": 40, "acierto": 0.2}, "auto": auto, "por": "automático",
                        "reversible": True, "deshacer": {"accion": inversa, "id": clave}})

    def accion(self, msg):
        fila = self.filas[msg["id"]]
        nuevo = {"restaurar": fila.previo, **{v: k for k, v in ESTADO_ACC.items()}}.get(msg["accion"])
        if nuevo is None:
            return {"ok": False, "error": "Acción desconocida"}
        self.cambiar(msg["id"], nuevo, f"A mano: {msg.get('motivo')}", auto=False)
        return {"ok": True}


async def test_fuentes_vivas_conectado(env):
    store, bus, _ = env
    assert E.desde_fuentes({"accion": "activar", "id": "x", "antes": "prueba", "despues": "activa", "motivo": "m"})[
        "tipo"] == "peso_cambiado"
    ev = evo(env)
    fv = FakeFuentes()
    ev.conectar_fuentes(fv)
    fv.cambiar("medio:ruido.net", "silenciada", "Acierta 8 de 40 (20 %)")
    e = E.leer(store)[0]
    assert e["tipo"] == "fuente_silenciada" and e["sujeto"] == "ruido.net" and e["origen"] == "fuentes_vivas"
    assert e["antes"] == {"estado": "activa", "peso": 1.0} and e["despues"] == {"estado": "silenciada", "peso": 0.0}
    assert e["evidencia"]["casos"] == 40 and e["evidencia"]["fuente_id"] == "medio:ruido.net"
    res = await ev.revertir(e["id"])
    assert res["ok"] and fv.filas["medio:ruido.net"].estado == "activa"
    back = E.leer(store)[0]
    assert back["tipo"] == "fuente_restaurada" and back["revierte"] == e["id"]
    assert E.leer(store, id_=e["id"])[0]["revertida_por"] == back["id"]
    await asyncio.sleep(1.1)                                                  # publicación agrupada
    assert bus.state["evolucion"]["cambios"][0]["id"] == back["id"]


async def test_fuentes_vivas_solo_revierte_lo_que_vuelve_a_antes(env):
    store, bus, _ = env
    ev = evo(env)
    fv = FakeFuentes()
    ev.conectar_fuentes(fv)
    fv.cambiar("medio:nuevo.io", "activa", "Supera la prueba")                # prueba → activa («activar»)
    act = E.leer(store)[0]
    assert act["tipo"] == "peso_cambiado" and act["evidencia"]["deshacer"]["accion"] == "a_prueba"
    assert (await ev.revertir(act["id"]))["ok"] and fv.filas["medio:nuevo.io"].estado == "prueba"
    # Una entrada que apuntó la versión anterior de fuentes_vivas (su inversa de «activar» era silenciar): se niega
    fv.filas["medio:nuevo.io"].estado = "activa"
    vieja = E.registrar(store, "peso_cambiado", "nuevo.io", {"estado": "prueba", "peso": 0.5},
                        {"estado": "activa", "peso": 1.0}, "Supera la prueba",
                        {"fuente_id": "medio:nuevo.io", "deshacer": {"accion": "silenciar", "id": "medio:nuevo.io"}},
                        "fuentes_vivas", ts=fv.ts + 5000)
    fv.ts += 10_000
    res = await ev.revertir(vieja["id"])
    assert not res["ok"] and "«prueba»" in res["why"] and fv.filas["medio:nuevo.io"].estado == "activa"
    # Cambios posteriores de la misma fuente: hay que deshacer antes el más reciente
    fv.cambiar("medio:ruido.net", "silenciada", "Acierta poco")
    sil = E.leer(store)[0]
    fv.cambiar("medio:ruido.net", "retirada", "Sigue mal")
    ret = E.leer(store)[0]
    res = await ev.revertir(sil["id"])                                        # deshacería también la retirada
    assert not res["ok"] and "otros cambios de esta fuente" in res["why"]
    assert (await ev.revertir(ret["id"]))["ok"] and fv.filas["medio:ruido.net"].estado == "silenciada"
    assert (await ev.revertir(sil["id"]))["ok"] and fv.filas["medio:ruido.net"].estado == "activa"   # ya se puede
    # Cambiada por fuera del historial: no se toca
    fv.cambiar("medio:ruido.net", "silenciada", "Otra vez")
    otra = E.leer(store)[0]
    fv.filas["medio:ruido.net"].estado = "retirada"
    res = await ev.revertir(otra["id"])
    assert not res["ok"] and "ya no está silenciada" in res["why"]


async def test_aprendizaje_conectado(env):
    from bot.aprendizaje import Aprendizaje
    store, bus, _ = env
    lunes = ms(2026, 11, 2)
    reloj = [ms(2026, 10, 29, 12)]
    apr = Aprendizaje(bus, store, clock=lambda: reloj[0])
    cifras = {"casos": 40.0, "independientes": 31, "acierto_aj": 0.62, "veredicto": "bueno"}
    store.db.execute("INSERT INTO medios_peso_hist(ts, outlet, antes, despues, tipo, motivo, cuerpo) VALUES "
                     "(?,?,?,?,?,?,?)", (reloj[0], "theblock.co", 1.0, 1.25, "auto",
                                         "Acierta el 62 % ajustado al mercado en 31 casos", json.dumps(cifras)))
    store.db.commit()
    apr.fijar_peso("humo.io", 0.4, "Solo dice tonterías")
    reloj[0] += H
    apr.fijar_peso("coindesk.com", 1.3, "Da antes que nadie")
    ev = evo(env, clock=lambda: lunes + 8 * H)
    ev.conectar_aprendizaje(apr)
    assert await ev.sincronizar_aprendizaje() == 3 and await ev.sincronizar_aprendizaje() == 0
    hb = E.leer(store, sujeto="theblock.co")[0]
    assert hb["tipo"] == "peso_cambiado" and hb["origen"] == "aprendizaje" and hb["ts"] == ms(2026, 10, 29, 12)
    assert hb["antes"] == {"peso": 1.0} and hb["despues"] == {"peso": 1.25} and hb["evidencia"]["acierto_aj"] == 0.62
    cd = E.leer(store, sujeto="coindesk.com")[0]
    res = await ev.revertir(cd["id"])                                         # lo deshace el aprendizaje
    assert res["ok"], res
    back = E.leer(store, sujeto="coindesk.com")[0]
    assert back["revierte"] == cd["id"] and back["despues"] == {"peso": 1.0}
    assert apr.weights["coindesk.com"]["peso"] == 1.0
    assert E.leer(store, id_=cd["id"])[0]["revertida_por"] == back["id"]
    hu = E.leer(store, sujeto="humo.io")[0]
    reloj[0] += H
    apr.fijar_peso("humo.io", 0.2, "Sigue sin acertar")
    res = await ev.revertir(hu["id"])                                         # hubo otro cambio después
    assert not res["ok"] and "otros cambios de peso" in res["why"]
    e = await ev.informe_semanal("2026-W44")
    txt = e["despues"]["texto"]
    assert "Suben: theblock.co (1,00 → 1,25, acierto 62 % en 40 casos)" in txt
    assert "Bajan: humo.io (1,00 → 0,20, 2 cambios)" in txt and "coindesk.com (1,00 → 1,30" not in txt
    assert "Da antes que nadie" in txt                                        # qué cambió y por qué


async def test_veredictos_del_aprendizaje_y_que_se_puede_revertir(env):
    """Las filas «veredicto» de medios_peso_hist (el peso no cambia) entran como «veredicto_medio», no se ofrecen para
    revertir ni bloquean revertir el cambio de peso anterior. Cada entrada del mensaje y de la API dice si es
    reversible: el informe, una propuesta descartada, un veredicto o lo que gestiona un módulo sin enlazar, no."""
    from bot.aprendizaje import Aprendizaje
    store, bus, _ = env
    t = ms(2026, 10, 29, 12)
    apr = Aprendizaje(bus, store, clock=lambda: t + 2 * H)
    sql = "INSERT INTO medios_peso_hist(ts, outlet, antes, despues, tipo, motivo, cuerpo) VALUES (?,?,?,?,?,?,?)"
    store.db.execute(sql, (t, "theblock.co", 1.0, 1.25, "auto", "Acierta (veredicto: neutro → acierta)",
                           json.dumps({"veredicto": "acierta"})))
    store.db.execute(sql, (t + H, "theblock.co", 1.25, 1.25, "veredicto", "Sigue (veredicto: acierta → falla)",
                           json.dumps({"veredicto": "falla"})))
    store.db.commit()
    viejo = E.registrar(store, "peso_cambiado", "x.com", {"peso": 1.0}, {"peso": 1.0}, "Copiado antes",
                        {"cambio_id": 999, "tipo_cambio": "veredicto"}, "aprendizaje", ts=t - H)
    desc = E.registrar(store, "propuesta_descartada", "ventana china", {"estado": "pendiente"}, {"estado": "descartada"},
                       "Descartada por ti", {"propuesta": "p1"}, "web", ts=t - 2 * H)
    fuente = E.registrar(store, "fuente_silenciada", "ruido.net", {"estado": "activa"}, {"estado": "silenciada"},
                         "Ruido", {"fuente_id": "medio:ruido.net"}, "fuentes_vivas", ts=t - 3 * H)
    promo = E.registrar(store, "estrategia_promovida", "E2", {"nombre": "A", "params": {}}, {"nombre": "B", "params": {}},
                        "Promoción", {}, "evolucion", ts=t - 4 * H)
    ev = evo(env, clock=lambda: ms(2026, 11, 2, 8))
    ev.conectar_aprendizaje(apr)
    assert await ev.sincronizar_aprendizaje() == 2
    ver, peso = E.leer(store, sujeto="theblock.co")
    assert ver["tipo"] == "veredicto_medio" and ver["etiqueta"] == "Veredicto de un medio" and ver["origen"] == "aprendizaje"
    assert ver["antes"] == {"veredicto": "acierta"} and ver["despues"] == {"veredicto": "falla", "peso": 1.25}
    assert peso["tipo"] == "peso_cambiado" and peso["despues"] == {"peso": 1.25}
    vista = {x["id"]: x["reversible"] for x in (await ev.api_historial())["cambios"]}
    assert vista == {ver["id"]: False, peso["id"]: True, viejo["id"]: False, desc["id"]: False,
                     fuente["id"]: False, promo["id"]: True}   # fuentes_vivas sin enlazar: no se ofrece
    ev._publicar(ev.clock())
    assert {x["id"]: x["reversible"] for x in bus.state["evolucion"]["cambios"]} == vista
    for e in (ver, viejo, desc):
        res = await ev.revertir(e["id"])
        assert not res["ok"] and "apunte del historial" in res["why"]
    res = await ev.revertir(peso["id"])                                       # el veredicto posterior no lo impide
    assert res["ok"], res
    back = E.leer(store, sujeto="theblock.co")[0]
    assert back["tipo"] == "peso_cambiado" and back["revierte"] == peso["id"]
    assert not ev.reversible(E.leer(store, id_=peso["id"])[0])                 # ya revertido


async def test_al_arrancar_espera_a_que_la_vigilancia_publique_para_evaluar(env):
    """Tras un reinicio el lunes, la evaluación y el informe de la semana esperan a las estrategias de la vigilancia
    (solo se refresca): si no, se guardaría para siempre un informe sin aspirantes ni gemelo."""
    store, bus, _ = env
    now = [ms(2026, 11, 2, 8)]
    ev = evo(env, clock=lambda: now[0])
    bus.publish({"type": "vigilancia", "estado": "arrancando", "estrategias": []})
    await ev.paso()
    assert (await ev.api_informes())["informes"] == [] and ev._estado("ultima_evaluacion") is None
    assert bus.state["evolucion"]["estado"] == "sin_datos"                    # pero sí refrescó la web
    now[0] += 5 * 60_000
    bus.publish({"type": "vigilancia", "estado": "arrancando", "estrategias": [{"id": ASP, "codigo": "E2"}]})
    await ev.paso()                                                           # con lista pero aún arrancando
    assert (await ev.api_informes())["informes"] == []
    now[0] += 5 * 60_000
    meta(bus, [(LAB_REF, "E1"), (LAB7, "E1-7d", {"en_paper": True, "variante_de": LAB_REF}), (ASP, "E2")])
    await ev.paso()
    [inf] = (await ev.api_informes())["informes"]
    assert sorted(a["codigo"] for a in inf["cifras"]["aspirantes"]) == ["E1", "E2"]
    assert "No hay aspirantes en la vigilancia todavía" not in inf["texto"] and ev._estado("ultima_evaluacion") == now[0]


async def test_sin_vigilancia_se_evalua_tras_la_espera_o_si_esta_apagada(env):
    store, bus, _ = env
    now = [ms(2026, 11, 2, 8)]
    ev = evo(env, clock=lambda: now[0])
    await ev.paso()                                                           # la vigilancia no ha dicho nada
    assert (await ev.api_informes())["informes"] == []
    now[0] += E.VIG_ESPERA_S * 1000 - 1
    await ev.paso()
    assert (await ev.api_informes())["informes"] == []
    now[0] += 1
    await ev.paso()                                                           # 15 min: se sigue sin ella
    assert len((await ev.api_informes())["informes"]) == 1
    now[0] += 7 * DAY                                                         # la semana siguiente, apagada: sin esperar
    bus.publish({"type": "vigilancia", "estado": "apagada", "estrategias": []})
    await ev.paso()
    assert len((await ev.api_informes())["informes"]) == 2


# ---------------------------------------------------------------- estrategia cambiada por el usuario (activar_estrategia)

def _script():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "scripts" / "activar_estrategia.py"
    spec = importlib.util.spec_from_file_location("activar_estrategia", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _url_cerrada() -> str:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()   # nada escucha ahí: el «bot» no está en marcha
    return f"http://127.0.0.1:{port}/health"


async def test_activar_seis_por_decision_del_usuario_y_volver(env, monkeypatch):
    from bot import apertura as ap
    store, bus, path = env
    m = _script()
    monkeypatch.setattr(m, "bot_en_marcha", lambda url, timeout=2.0: False)   # la comprobación real va aparte
    ahora = ms(2026, 10, 28, 20, 0)
    datos = ["--datos", str(path.parent), "--url", _url_cerrada()]
    assert m.main(["--preset", "seis", "--simular", *datos], ahora=ahora) == 0 and not path.exists()
    assert m.main(["--preset", "seis", *datos], ahora=ahora) == 0
    act = E.leer_estrategia_activa(path)
    assert act["preset"] == "seis" and act["nombre"] == "6 diarias" and act["fuente"] == "usuario" and act["solo_paper"]
    assert E.validar_activa(act) == (True, None) and act["firma"] == E.firma_estrategia(act) and not act["pausada"]
    assert act["lab_id"] is None and act["lab_twin"] is None and "lab" not in act["params"]
    p = ap.impulse_params(E.params_motor(act))[0]
    assert ap.preset_of(p) == "seis" and E.firma_motor(p) == act["firma"]
    [e] = E.leer(store, tipo="estrategia_cambiada")
    assert "Estrategia cambiada por decisión del usuario el 28-10-2026: 6 diarias" in e["motivo"] and "paper" in e["motivo"]
    assert e["origen"] == "usuario" and e["antes"]["fuente"] == "preset" and e["despues"]["preset"] == "seis"
    assert e["id"] == act["registro"] and e["evidencia"]["firma_despues"] == act["firma"]
    row = store.db.execute("SELECT valor FROM evolution_state WHERE clave='ultimo_cambio'").fetchone()
    assert json.loads(row[0]) == ahora   # la evolución automática espera 14 días para promocionar otra
    # El motor la carga: preset «seis» con la firma del archivo
    monkeypatch.setenv("BOT_APERTURAS_ESTRATEGIA", "impulso")
    monkeypatch.delenv("BOT_IMPULSO_PARAMS", raising=False)
    eng = ap.OpeningEngine(bus, None, lambda c, w: {"dir": 0}, lambda *a: True, lambda: {}, clock=lambda: ahora,
                           activa_path=path)
    st = eng.view()
    assert eng.source == "usuario" and eng.firma == act["firma"] and st["params"]["preset"] == "seis"
    assert st["params_source"] == "usuario" and st["estrategia_cargada"]["fuente"] == "usuario"
    assert st["params"]["nombre"] == "6 diarias" and st["day"]["max_entries"] == 6
    # Otra vez lo mismo: nada que cambiar (no se repite en el historial)
    assert m.main(["--preset", "seis", *datos], ahora=ahora + 1000) == 0
    assert len(E.leer(store, tipo="estrategia_cambiada")) == 1
    # Se puede revertir desde la evolución (vuelve a lo de antes: el preset)
    ev = evo(env, clock=lambda: ahora + 2000)
    assert ev.reversible(e)
    res = await ev.revertir(e["id"])
    assert res["ok"], res
    back = E.leer_estrategia_activa(path)
    assert E.es_preset(back) and E.params_motor(back) is None
    rev = E.leer(store, tipo="estrategia_cambiada")[0]
    assert rev["revierte"] == e["id"]
    # Y con el script, a E2 (el preset de siempre), apuntado también
    assert m.main(["--preset", "seis", *datos], ahora=ahora + 3000) == 0
    assert m.main(["--preset", "impulso", *datos], ahora=ahora + 4000) == 0
    imp = E.leer_estrategia_activa(path)
    assert imp["preset"] == "impulso" and E.es_preset(imp) and imp["nombre"] == "E2 · impulso"
    assert "E2 · impulso" in E.leer(store, tipo="estrategia_cambiada")[0]["motivo"]


def test_activar_se_niega_con_el_bot_en_marcha(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Ok(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Ok)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        m = _script()
        url = f"http://127.0.0.1:{srv.server_address[1]}/health"
        assert m.bot_en_marcha(url) and not m.bot_en_marcha(_url_cerrada())
        assert m.main(["--preset", "seis", "--datos", str(tmp_path), "--url", url]) == 2
        assert not (tmp_path / E.ARCHIVO).exists() and not (tmp_path / "bot.db").exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_activar_no_escribe_lo_que_rompe_las_reglas(tmp_path, monkeypatch):
    from bot import apertura as ap
    m = _script()
    monkeypatch.setitem(ap.PRESETS, "seis", {**ap.PRESETS["seis"], "params": {**ap.SEIS, "sl_pct": 1.0}})
    res = m.activar("seis", tmp_path, ahora=ms(2026, 10, 28, 20))
    assert not res["ok"] and "reglas del usuario" in res["why"] and not (tmp_path / E.ARCHIVO).exists()
    assert not m.activar("otro", tmp_path)["ok"]


def test_activar_sin_bot_db_no_crea_nada(tmp_path):
    """--datos que no apunta a la carpeta del bot: no se crea una bot.db vacía ni se escribe la estrategia."""
    m = _script()
    res = m.activar("seis", tmp_path, ahora=ms(2026, 10, 28, 20))
    assert not res["ok"] and "bot.db" in res["why"]
    assert not (tmp_path / "bot.db").exists() and not (tmp_path / E.ARCHIVO).exists()


def test_activar_es_todo_o_nada(env):
    """Si falla el apunte del último cambio o el del historial, no queda ninguno de los dos y el archivo vuelve."""
    store, _bus, path = env
    m = _script()
    E.asegurar_tablas(store)
    store.db.execute("CREATE TRIGGER sin_estado BEFORE INSERT ON evolution_state BEGIN SELECT RAISE(ABORT, 'no'); END")
    store.db.commit()
    res = m.activar("seis", path.parent, ahora=ms(2026, 10, 28, 20))
    assert not res["ok"] and "historial" in res["why"] and not path.exists()
    assert E.leer(store, tipo="estrategia_cambiada") == []
    store.db.execute("DROP TRIGGER sin_estado")
    store.db.execute("CREATE TRIGGER sin_log BEFORE INSERT ON evolution_log BEGIN SELECT RAISE(ABORT, 'no'); END")
    store.db.commit()
    res = m.activar("seis", path.parent, ahora=ms(2026, 10, 28, 21))
    assert not res["ok"] and not path.exists()
    assert store.db.execute("SELECT COUNT(*) FROM evolution_state WHERE clave='ultimo_cambio'").fetchone()[0] == 0
