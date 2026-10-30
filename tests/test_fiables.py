"""Noticias fiables (bot/fiables.py): criterios fijados de antemano, ajuste al mercado, constancia, días completos, azar,
tabla de solo añadir, ruta /api/noticias_fiables y fiable_para. Sin red."""
import sqlite3
from datetime import datetime, timezone

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot import aprendizaje, fiables
from bot.bus import Bus
from bot.server import Services, make_app
from bot.store import Store

MIN, H, DAY = 60_000, 3_600_000, 86_400_000
NOW = int(datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)   # 27 completo; 28 no
D23 = int(datetime(2026, 10, 23, tzinfo=timezone.utc).timestamp() * 1000) // DAY


@pytest.fixture(autouse=True)
def sin_estado():
    fiables.usar(None)
    yield
    fiables.usar(None)


def fila(story, day, outlet, d, acc, coin="BTC", rol="primera", tipo="exchange", fuente=None, acc60=None, k=0,
         orden=0, senal=1, sube=None, baja=None, r1440=None, base_d=None):
    return {"story": story, "coin": coin, "outlet": outlet, "rol": rol, "orden": orden, "fuente": fuente,
            "nombre": f"Feed {fuente}" if fuente else None, "tipo": tipo, "dir": d,
            "t0": ((D23 if base_d is None else base_d) + day) * DAY + 10 * H + k * MIN, "acc60": acc60, "acc240": acc,
            "senal": senal, "max_sube": sube, "max_baja": baja, "r1440": r1440}


def dia(day, outlet, hits, misses, tipo, fuente=None, mixto=True):
    """hits aciertos y misses fallos de un medio en un día (mitad alcistas y mitad bajistas si mixto)."""
    out = []
    for j in range(hits + misses):
        d = -1 if mixto and j % 2 else 1
        acc = d * 1.0 if j < hits else -d * 1.0
        out.append(fila(f"{outlet}-{day}-{j}", day, outlet, d, acc, tipo=tipo, fuente=fuente, acc60=acc / 2, k=j))
    return out


def con_deriva_cero(rows):
    """Una fila sin dirección por moneda y día que deja la media de acc240 (y de acc60) en 0."""
    sums: dict = {}
    for r in rows:
        k = (r["coin"], r["t0"] // DAY)
        s = sums.setdefault(k, [0.0, 0.0])
        s[0] += r["acc240"]
        s[1] += r["acc60"] or 0.0
    extra = [fila(f"relleno-{c}-{dd}", dd - D23, "relleno.org", 0, -s240, coin=c, acc60=-s60)
             for (c, dd), (s240, s60) in sums.items()]
    return rows + extra


def datos():
    rows = []
    for day in (0, 2, 3, 4):   # 23, 25, 26 y 27 de octubre (el 24 falta, como en los datos reales)
        rows += dia(day, "bueno.com", 6, 2, "exchange", fuente="feed-bueno")      # 75 % cada día
        rows += dia(day, "malo.com", 4, 4, "macro")                                # 50 %
        rows += dia(day, "irregular.com", 2 if day == 3 else 8, 6 if day == 3 else 0, "regulatorio")  # un día flojo
    rows += dia(0, "pocos.com", 6, 0, "seguridad") + dia(2, "pocos.com", 6, 0, "seguridad")   # 100 % sin muestra
    rows += dia(5, "bueno.com", 0, 8, "exchange", fuente="feed-bueno")   # hoy (28): no cuenta, aún no está completo
    return con_deriva_cero(rows)


def grupo(msg, cl):
    return next((g for g in msg["grupos"] if g["clave"] == cl), None)


def test_wilson_y_criterios_fijos():
    assert fiables.wilson_bajo(24, 32) == pytest.approx(0.608, abs=0.002)
    assert fiables.wilson_bajo(0, 0) == 0.0 and fiables.wilson_bajo(30, 30) > 0.9
    c = fiables.CRITERIOS
    assert (c["n_min"], c["dias_min"], c["n_dia"], c["acierto_min"], c["wilson90_min"], c["dia_min"]) == \
        (30, 3, 5, 0.60, 0.50, 0.50)
    assert c["z"] == pytest.approx(1.645) and fiables.PERMS >= 50


def test_calculo_fiable_constancia_muestra_y_dias_completos():
    msg = fiables.calcular(datos(), NOW, perms=50)
    assert msg["type"] == "noticias_fiables" and msg["desde"] == "2026-10-23" and msg["hasta"] == "2026-10-27"
    assert msg["dias"] == 4 and msg["dias_lista"] == ["2026-10-23", "2026-10-25", "2026-10-26", "2026-10-27"]
    assert msg["version"] == fiables.VERSION and msg["horas_por_dia"][0] == {"dia": "2026-10-23", "horas": 1}
    b = grupo(msg, "medio:bueno.com")
    assert b["estado"] == "fiable" and b["n"] == 32 and b["dias"] == 4 and b["dias_validos"] == 4   # sin el día 28
    assert b["acierto"] == 0.75 and b["acierto_bruto"] == 0.75 and b["acierto_1h"] == 0.75 and b["ic90_bajo"] >= 0.5
    assert [x["acierto"] for x in b["por_dia"]] == [0.75] * 4 and b["sentido"] is None and b["outlet"] == "bueno.com"
    assert b["n_historias"] == 32 and [x["n_historias"] for x in b["por_dia"]] == [8] * 4
    # el feed es el único de su medio (y el medio solo tiene ese feed): el mismo grupo, no se prueba dos veces
    assert grupo(msg, "fuente:feed-bueno") is None and msg["duplicados"] == 1
    assert msg["familias"]["fuente"]["duplicados"] == 1 and msg["familias"]["fuente"]["pasan"] == 0
    assert grupo(msg, "tipo_sentido:exchange:1")["n"] == 16   # medio×sentido y tipo×sentido: 4 por día, < 5
    assert grupo(msg, "medio_sentido:bueno.com:1")["estado"] == "en_prueba"   # 75 % sin días con ≥ 5
    irr = grupo(msg, "medio:irregular.com")   # 26 de 32 (81 %), pero un día con 2 de 8: no es constante
    assert irr["acierto"] == pytest.approx(26 / 32, abs=1e-4) and irr["estado"] == "no_fiable"
    assert grupo(msg, "medio:malo.com")["estado"] == "no_fiable"
    assert grupo(msg, "medio:pocos.com")["estado"] == "en_prueba"   # 100 % con 12: sin muestra todavía
    assert msg["pasan"] == 1 and msg["familias"]["medio"]["pasan"] == 1
    assert msg["grupos"][0]["clave"] == "medio:bueno.com"
    assert msg["permutaciones"] == 50 and 0 <= msg["por_azar"] < msg["pasan"] and 0 < msg["p_azar"] <= 1
    assert set(msg["azar_bloques"]) == {"media", "p95", "max", "p", "bloque_min"}
    assert "1 de" in msg["nota"] and "dentro de cada hora" in msg["nota"] and "puerta para operar" in msg["nota"]
    assert msg["probados"] >= 8 and all(g["n"] >= fiables.MIN_N_LISTA for g in msg["grupos"])
    # sin días posteriores a su elección con noticias en vivo suficientes (y con el azar por horas): no opera
    assert b["opera"] is False and b["no_opera"] and msg["operan"] == 0


def test_ajuste_al_mercado_y_una_fila_por_historia():
    # Día bajista: todo cae un 1 %. Las bajistas aciertan en bruto, pero no más que el mercado: acierto ajustado 0.
    rows = [fila(f"b{j}", 0, "bajista.com", -1, -1.0, coin="ETH", k=j) for j in range(12)]
    # Una historia con su primer medio y una confirmación: cuenta una vez, para el primero
    rows += [fila("h1", 0, "primero.com", 1, 2.0, coin="SOL", k=50),
             fila("h1", 0, "copia.com", 1, 2.0, coin="SOL", rol="confirmacion", orden=1, k=51)]
    u = fiables.unidades(rows)
    assert len(u) == 13 and {r["outlet"] for r in u if r["story"] == "h1"} == {"primero.com"}
    msg = fiables.calcular(rows, NOW, perms=50)
    g = grupo(msg, "medio:bajista.com")
    assert g["acierto_bruto"] == 1.0 and g["acierto"] == 0.0
    assert grupo(msg, "medio:copia.com") is None


def test_sin_datos_y_fiable_para():
    vacio = fiables.calcular([], NOW, perms=50)
    assert vacio["pasan"] == 0 and vacio["operan"] == 0 and vacio["grupos"] == [] and "Sin noticias" in vacio["nota"]
    fiables.usar(vacio)
    assert not fiables.hay_fiables() and fiables.fiable_para("bueno.com", None, "exchange", 1) is None
    msg = fiables.calcular(datos(), NOW, perms=50)   # un grupo fiable, pero no pasa la puerta para operar
    fiables.usar(msg)
    assert grupo(msg, "medio:bueno.com")["estado"] == "fiable" and fiables.ultimo() is msg
    assert not fiables.hay_fiables() and fiables.fiable_para("bueno.com", None, "macro", -1) is None
    viejo = {k: v for k, v in grupo(msg, "medio:bueno.com").items() if k != "opera"}
    fiables.usar({"grupos": [viejo]})   # sin «opera»: un cálculo de antes de la puerta no pone ninguno
    assert not fiables.hay_fiables()
    pu = fiables.calcular(datos_puerta(), NOW2, perms=300)
    fiables.usar(pu)
    assert fiables.hay_fiables() and pu["operan"] >= 1
    g = fiables.fiable_para("bueno.com", None, "macro", -1)
    assert g["estado"] == "fiable" and g["opera"] is True and g["clave"] in ("medio:bueno.com",
                                                                            "medio_sentido:bueno.com:-1")
    assert fiables.fiable_para("malo.com", None, "macro", 1) is None
    assert fiables.fiable_para("bueno.com", None, None, 0) is None   # sin dirección, nada


def datos_puerta(senal=1):
    """Ocho días (23 a 30 de octubre) de un medio que acierta el 75 % con 12 noticias al día, cada una en su hora y
    acompañada de tres noticias de otro medio en la misma hora (dos alcistas y dos bajistas por hora en total) que no
    aciertan nunca. La horquilla: los aciertos llegan a +5 % sin tocar el stop y los fallos tocan el −2 %."""
    rows = []
    for day in range(8):
        for j in range(12):
            d = -1 if j % 2 else 1
            ok = j < 9
            acc = d * 1.0 if ok else -d * 1.0
            fav, contra = (5.0, 0.5) if ok else (0.3, 2.0)
            sube, baja = (fav, contra) if d > 0 else (contra, fav)
            k = (j - 4) * 60   # de 6:00 a 17:00
            rows.append(fila(f"b-{day}-{j}", day, "bueno.com", d, acc, tipo="exchange", acc60=acc, k=k, sube=sube,
                             baja=baja, r1440=acc, senal=senal))
            for i, dn in enumerate((-d, -d, d)):
                rows.append(fila(f"r-{day}-{j}-{i}", day, "ruido.com", dn, 0.0, tipo="macro", acc60=0.0,
                                 k=k + 10 + 10 * i))
    return con_deriva_cero(rows)


NOW2 = int(datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)   # completos hasta el 30


def test_puerta_para_operar_azar_por_horas_historias_y_hacia_delante():
    msg = fiables.calcular(datos_puerta(), NOW2, perms=300)
    b = grupo(msg, "medio:bueno.com")
    assert b["estado"] == "fiable" and b["n"] == b["n_historias"] == 96 and b["bloques"] == 96
    assert b["acierto"] == 0.75 and b["acierto_bruto"] == 0.75 and b["ic90_bajo_bloques"] > 0.6
    assert b["horquilla_R"] == pytest.approx(2.0) and b["n_horquilla"] == 96   # 75 % × 3R − 25 % × 1R
    # elegido por primera vez el 26 con solo el 23, el 24 y el 25; hacia delante, del 26 al 30 (60 noticias en vivo)
    assert b["adelante"] == {"desde": "2026-10-26", "n": 60, "n_historias": 60, "acierto": 0.75, "acierto_bruto": 0.75}
    assert b["vivo"]["n"] == 96 and b["opera"] is True and b["no_opera"] == []
    assert msg["azar_bloques"]["p"] < 0.05 and msg["azar_bloques"]["media"] < 1 and msg["operan"] >= 1
    assert grupo(msg, "medio:ruido.com")["estado"] == "no_fiable"
    assert "también la puerta para operar" in msg["nota"]
    # lo mismo con noticias que el bot no usaría en vivo (senal 0): hacia delante no hay nada y no opera
    g = grupo(fiables.calcular(datos_puerta(senal=0), NOW2, perms=50), "medio:bueno.com")
    assert g["estado"] == "fiable" and g["opera"] is False and g["vivo"]["n"] == 0
    assert any("hacia delante y en vivo" in x for x in g["no_opera"])
    # una macro que la cripto aún no confirmó (senal 2) cuenta en vivo como la 1: quedarse solo con las confirmadas
    # miraría el futuro (la confirmación es un movimiento a favor dentro de los 60 min que también mide acc240)
    m2 = grupo(fiables.calcular(datos_puerta(senal=2), NOW2, perms=50), "medio:bueno.com")
    assert m2["vivo"]["n"] == 96 and m2["adelante"]["n"] == 60
    assert not any("hacia delante" in x for x in m2["no_opera"])
    # cada noticia sola en su hora (sin las del otro medio): el azar que conserva la hora no la distingue
    sola = fiables.calcular(con_deriva_cero([r for r in datos_puerta() if r["outlet"] == "bueno.com"]), NOW2,
                            perms=50)
    sg = grupo(sola, "medio:bueno.com")
    assert sg["estado"] == "fiable" and sg["opera"] is False and sola["azar_bloques"]["p"] >= 0.05
    assert any("azar que conserva la hora" in x for x in sg["no_opera"])


def test_acierto_bruto_y_horquilla_en_la_puerta():
    # los largos tocan el stop y los cortos no llegan al objetivo: la horquilla del usuario pierde aunque acierte
    malas = [{**r, "max_sube": 0.3, "max_baja": 2.0} if r["outlet"] == "bueno.com" else r for r in datos_puerta()]
    g = grupo(fiables.calcular(malas, NOW2, perms=50), "medio:bueno.com")
    assert g["horquilla_R"] < 0 and g["opera"] is False and any("horquilla" in x for x in g["no_opera"])
    ok = {"estado": "fiable", "n_historias": 40, "ic90_bajo_bloques": 0.6, "bloques": 40, "acierto_bruto": 0.6,
          "n_horquilla": 40, "horquilla_R": 0.2,
          "adelante": {"desde": "2026-10-26", "n_historias": 40, "acierto": 0.65, "acierto_bruto": 0.55}}
    pu = fiables.PUERTA
    assert fiables._no_opera(ok, 0.01, pu) == []
    assert fiables._no_opera({**ok, "acierto_bruto": 0.34}, 0.01, pu) == [
        "acierto bruto 34 %: sin cobertura, una operación perdería"]
    assert "azar que conserva la hora" in fiables._no_opera(ok, 0.2, pu)[0]
    assert fiables._no_opera({**ok, "adelante": {**ok["adelante"], "acierto": 0.41}}, 0.01, pu) == [
        "hacia delante y en vivo acierta el 41 %"]
    assert fiables._no_opera({**ok, "n_historias": 9}, 0.01, pu) == ["9 noticias distintas (hacen falta 30)"]
    assert fiables._no_opera({**ok, "estado": "en_prueba"}, 0.01, pu) == ["no pasa los criterios fijados"]


def test_noticia_de_cuatro_monedas_cuenta_una_vez_como_noticia():
    rows = []
    for day in (0, 2, 3, 4):
        for j in range(8):
            for coin in ("BTC", "ETH", "SOL", "XRP"):
                rows.append(fila(f"m-{day}-{j}", day, "macro.com", 1, 1.0 if j < 6 else -1.0, coin=coin, tipo="macro",
                                 k=j * 45))
    msg = fiables.calcular(con_deriva_cero(rows), NOW, perms=50)
    g = grupo(msg, "medio:macro.com")
    assert g["n"] == 128 and g["n_historias"] == 32 and [x["n_historias"] for x in g["por_dia"]] == [8] * 4
    assert g["bloques"] == 32 and g["ic90_bajo_bloques"] < g["ic90_bajo"]   # 32 casos, no 128
    assert g["opera"] is False   # sin días posteriores a su elección


def test_valor_p_horquilla_y_constancia_por_semanas():
    assert fiables.valor_p([0, 0, 1], 1) == pytest.approx(0.5) and fiables.valor_p([0, 0], 0) == 1.0
    assert fiables.valor_p([], 3) == 1.0
    assert fiables.horquilla_R(1, 5.0, 0.5, 4.0) == pytest.approx(3.0)
    assert fiables.horquilla_R(-1, 5.0, 2.0, -1.0) == -1.0 and fiables.horquilla_R(1, 5.0, 2.0, 3.0) == -1.0
    assert fiables.horquilla_R(1, 1.0, 1.0, 0.75) == pytest.approx(0.5) and fiables.horquilla_R(1, 1, 1, None) is None
    base = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp() * 1000) // DAY - D23   # desplazamiento
    rows = []
    for day in range(15):   # 1 a 15 de octubre; el 6 (martes) acierta 2 de 8
        hits = 2 if day == 5 else 6
        rows += [fila(f"s-{day}-{j}", base + day, "semanal.com", 1 if j % 2 == 0 else -1,
                      (1.0 if j % 2 == 0 else -1.0) * (1 if j < hits else -1), k=j * 40) for j in range(8)]
    rows = con_deriva_cero(rows)
    quince = int(datetime(2026, 10, 16, 12, tzinfo=timezone.utc).timestamp() * 1000)
    diez = int(datetime(2026, 10, 11, 12, tzinfo=timezone.utc).timestamp() * 1000)
    assert grupo(fiables.calcular(rows, quince, perms=50), "medio:semanal.com")["estado"] == "fiable"   # por semanas
    assert grupo(fiables.calcular(rows, diez, perms=50), "medio:semanal.com")["estado"] == "no_fiable"  # por días


def test_monedas_de_una_noticia_en_vivo():
    cripto = {"repercusion": {"direccion": 60, "activos": ["SOL"]}, "monedas": ["SOL"], "tipo": "exchange"}
    assert fiables.monedas_de("Solana sube", cripto, "cripto", None) == (["SOL"], "nombra la moneda")
    macro = {"repercusion": {"direccion": -60, "activos": []}, "tipo": "macro"}
    assert fiables.monedas_de("La Fed sube tipos", macro, "macro", None)[1] == "esperando_reflejo"
    assert fiables.monedas_de("La Fed sube tipos", macro, "macro", {"estado": "pendiente"})[1] == "esperando_reflejo"
    four, _ = fiables.monedas_de("La Fed sube tipos", macro, "macro", {"estado": "confirmada"})
    assert four == ["BTC", "ETH", "SOL", "XRP"]
    no, why = fiables.monedas_de("La Fed sube tipos", macro, "macro", {"estado": "contraria"})
    assert no == [] and "no confirmó" in why
    nada, why = fiables.monedas_de("Una empresa", {"repercusion": {"direccion": 50}, "tipo": "empresas"}, "contexto",
                                   None)
    assert nada == [] and "no nombra" in why


def _tabla(store, rows):
    store.db.executescript(aprendizaje.SCHEMA)
    cols = fiables.COLS
    store.db.executemany(f"INSERT INTO news_outcomes ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                         [tuple(r[c] for c in cols) for r in rows])
    store.db.commit()


async def test_modulo_guarda_publica_y_no_repite(tmp_path):
    store = Store(tmp_path / "bot.db")
    bus = Bus(store)
    _tabla(store, datos())
    f = fiables.Fiables(bus, store, clock=lambda: NOW, perms=50)
    assert f.view() is None and not fiables.hay_fiables()
    msg = await f.step()
    assert msg["pasan"] == 1 and bus.state["noticias_fiables"] is msg and not fiables.hay_fiables()   # no opera
    assert any(m.get("type") == "noticias_fiables" for m in bus.snapshot())   # estado inicial del WebSocket
    n = lambda: store.db.execute("SELECT COUNT(*) FROM noticias_fiables").fetchone()[0]
    assert n() == 1
    again = await f.step()   # mismos datos: mismo resultado, no se guarda otro igual
    assert again is msg and n() == 1
    with pytest.raises(sqlite3.DatabaseError):   # solo de añadir
        store.db.execute("DELETE FROM noticias_fiables")
    store.db.rollback()
    with pytest.raises(sqlite3.DatabaseError):
        store.db.execute("UPDATE noticias_fiables SET ts = 0")
    store.db.rollback()
    store.db.execute("INSERT INTO news_outcomes (story, coin, outlet, t0, dir, acc240) VALUES ('nueva', 'BTC', 'x.com', "
                     "?, 1, 0.5)", ((D23 + 4) * DAY + 15 * H,))
    store.db.commit()
    nuevo = await f.step()   # cambian los datos: cálculo nuevo y otro registro
    assert nuevo is not msg and n() == 2 and nuevo["unidades"] == msg["unidades"] + 1
    fiables.usar(None)
    g = fiables.Fiables(Bus(store), store, clock=lambda: NOW, perms=50)   # al arrancar: el último guardado, en uso
    assert g.view()["unidades"] == nuevo["unidades"] and fiables.ultimo() == g.view()
    store.close()


async def test_ruta_api_noticias_fiables(tmp_path):
    store = Store(tmp_path / "bot.db")
    bus = Bus(store)
    async with TestClient(TestServer(make_app(bus, lambda: {}))) as client:
        r = await client.get("/api/noticias_fiables")
        assert r.status == 404
    _tabla(store, datos())
    f = fiables.Fiables(bus, store, clock=lambda: NOW, perms=50)
    await f.step()
    async with TestClient(TestServer(make_app(bus, lambda: {}, Services(fiables=f)))) as client:
        r = await client.get("/api/noticias_fiables")
        assert r.status == 200
        d = await r.json()
        assert d["type"] == "noticias_fiables" and d["pasan"] == 1 and {"probados", "por_azar", "criterios", "nota",
                                                                         "grupos", "desde", "hasta", "dias", "operan",
                                                                         "azar_bloques", "puerta"} <= set(d)
        g = d["grupos"][0]
        assert {"clave", "tipo", "nombre", "sentido", "n", "dias", "acierto", "acierto_bruto", "acierto_1h",
                "ic90_bajo", "por_dia", "estado", "n_historias", "opera", "no_opera", "adelante", "vivo"} <= set(g)
    async with TestClient(TestServer(make_app(bus, lambda: {}))) as client:   # sin el módulo: el último del bus
        assert (await (await client.get("/api/noticias_fiables")).json())["pasan"] == 1
    store.close()
