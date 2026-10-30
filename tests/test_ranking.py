"""Ranking de estrategias (bot/ranking.py; contrato en docs/04, 2.20): la fila común, el orden por beneficio fuera de
muestra con costes, las que pierden fuera, la que está en marcha, lo de en vivo (paper y prueba en avance), el
laboratorio vivo pasado a jornadas, «nueva» y el puesto de antes, la recarga por mtime, los filtros, el mensaje del bus
y GET /api/ranking. Sin red y sin leer M:\\BotFundamental-datos (todo en tmp_path)."""
import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot import config, ranking
from bot.bus import Bus
from bot.models import now_ms
from bot.ranking import Ranking, de_tarjeta, entra, jornadas_entre, normalizar, ordenar
from bot.server import Services, make_app
from bot.store import Store


def fila(fid: str, R=0.1, n=100, op=1.0, dentro=0.05, origen="estudio", **kw) -> dict:
    return {"id": fid, "nombre": f"Estrategia {fid}", "origen": origen, "fuera": {"n": n, "R": R, "gana_pct": 40.0},
            "dentro": {"n": 200, "R": dentro, "gana_pct": 38.0}, "op_dia": op, "cumple_reglas": True, **kw}


def poner(car: Path, nombre: str, d, mtime: float | None = None) -> Path:
    car.mkdir(parents=True, exist_ok=True)
    p = car / nombre
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    if mtime is not None:   # el reloj de los archivos de Windows va a saltos: se fija a mano
        os.utime(p, (mtime, mtime))
    return p


def rk(tmp_path: Path, **kw) -> Ranking:
    kw.setdefault("carpeta", tmp_path / "ranking")
    kw.setdefault("lab_dir", tmp_path / "lab_vivo")
    kw.setdefault("estado_path", tmp_path / "ranking_estado.json")
    kw.setdefault("activa_path", tmp_path / "estrategia_activa.json")
    return Ranking(**kw)


def openings(preset: str | None, nombre: str = "X") -> dict:
    return {"type": "openings", "params": {"preset": preset, "nombre": nombre},
            "estrategia_cargada": {"preset": preset, "ts": 1_793_276_347_112}, "paused": False}


def test_jornadas_como_las_de_los_estudios():
    assert jornadas_entre("2025-08-16", "2026-10-20") == 306 and jornadas_entre("2023-10-30", "2025-08-15") == 468
    assert jornadas_entre("2026-01-01", "2026-01-01") == 0          # 1 de enero (jueves): no abre ninguna bolsa
    assert jornadas_entre("2026-10-31", "2026-11-01") == 0          # fin de semana
    assert jornadas_entre("2026-10-30", "2026-10-29") == 0 and ranking.dias_entre("2025-08-16", "2026-10-20") == 431
    assert ranking.holm([0.01, None, 0.04, 0.03]) == [0.03, None, 0.06, 0.06]


def test_jornadas_sin_recorrer_dia_a_dia_y_con_fechas_extremas():
    """Las mismas que contando día a día, para muchos tramos; y con fechas extremas, al momento y sin desbordar."""
    import random
    from datetime import date, timedelta

    def a_mano(a: date, b: date) -> int:
        n, d = 0, a
        while True:
            n += d.weekday() < 5 and (d.month, d.day) != (1, 1)
            if d >= b:
                return n
            d += timedelta(days=1)

    rnd = random.Random(7)
    for _ in range(400):
        a = date(2019, 12, 20) + timedelta(days=rnd.randrange(0, 2600))
        b = a + timedelta(days=rnd.randrange(0, 900))
        assert jornadas_entre(a.isoformat(), b.isoformat()) == a_mano(a, b), (a, b)
    t0 = time.perf_counter()
    assert jornadas_entre("0001-01-01", "9999-12-31") > 2_600_000 and time.perf_counter() - t0 < 0.5
    assert jornadas_entre("9999-12-30", "9999-12-31") == a_mano(date(9999, 12, 30), date(9999, 12, 31))


def test_normalizar_calcula_lo_que_falta_y_la_fragilidad():
    f = normalizar({"id": "a", "nombre": "A", "fuera": {"n": 612, "R": 0.2, "jornadas": 306}, "dentro": {"n": 10, "R": -0.1},
                    "robustez": {"sin_5_mayores": -0.05, "corregida": {"metodo": "Holm", "valor": 0.4, "pasa": False}}})
    assert f["op_dia"] == 2.0 and f["pct_dia"] == 0.2 and f["R_dia"] == 0.4   # 0,2 R × 0,5 % × 2 operaciones
    assert f["fragil"] and f["fragil_motivos"] == ["gana fuera de muestra pero pierde dentro",
                                                   "sin sus 5 mejores operaciones pierde",
                                                   "no pasa la corrección por número de pruebas"]
    assert f["robustez"]["dentro_positivo"] is False and f["estado"] == "backtest" and f["origen"] == "estudio"
    g = normalizar({"id": "b", "fuera": {"n": 12, "R": float("nan")}, "pct_dia": float("inf"), "estado": "de_moda",
                    "origen": "Otro Origen", "cumple_reglas": "sí"})
    assert g["fuera"]["R"] is None and g["pct_dia"] is None and g["estado"] == "backtest" and g["origen"] == "estudio"
    assert g["cumple_reglas"] is None and g["poca_muestra"] and g["nombre"] == "b" and not entra(g)
    sana = normalizar(fila("c", R=0.2, dentro=0.1, robustez={"sin_5_mayores": 0.05,
                                                             "corregida": {"metodo": "q", "valor": 0.05, "pasa": True}}))
    assert not sana["fragil"] and sana["fragil_motivos"] == [] and sana["pct_dia"] == 0.1
    for mala in (None, {}, {"id": ""}, {"id": 5}, [1, 2], "x"):
        assert normalizar(mala) is None
    m = normalizar({**fila("m", R=0.2, dentro=-0.1), "fragil": True, "fragil_motivos": ["otra razón", 5]})
    assert m["fragil_motivos"] == ["gana fuera de muestra pero pierde dentro", "otra razón"]   # se suman
    assert normalizar({**fila("s", R=0.2), "fragil": True})["fragil_motivos"] == ["marcada frágil por su estudio"]
    assert normalizar({**fila("s", R=0.2), "fragil": "sí"})["fragil"] is False   # solo el true de JSON


def test_sin_correccion_por_numero_de_pruebas_es_fragil_y_la_correccion_lleva_su_tipo():
    """Una de varias configuraciones sin corrección (las 378 salidas, E2 con 7 días…) no sale como la más limpia:
    corregida «sin_p» (no pasa) y frágil con su motivo. El tipo corto de cada corrección, para el chip de la web, se
    deduce del método en los archivos que no lo traen."""
    f = normalizar(fila("salida", R=0.17, dentro=0.006, probadas=378))
    assert f["robustez"]["corregida"] == {"tipo": "sin_p", "valor": None, "pasa": False,
                                          "metodo": "sin p ni corrección: una de 378 configuraciones de su estudio"}
    assert f["fragil"] and f["fragil_motivos"] == ["sin corrección por número de pruebas: una de 378 configuraciones de "
                                                   "su estudio, que sube en el ranking por su resultado fuera de muestra"]
    con_p = normalizar(fila("p", fuera={"n": 50, "R": 0.2, "p": 0.04}, probadas=2000))
    assert con_p["robustez"]["corregida"]["metodo"] == "p 0,040 sin corregir: una de 2.000 configuraciones de su estudio"
    for sola in (normalizar(fila("una", probadas=1)), normalizar(fila("nada"))):   # una sola configuración: sin chip
        assert sola["robustez"]["corregida"] is None and not sola["fragil"]
    e2 = normalizar(fila("e2", probadas=None, robustez={"corregida": {"tipo": "sin_p", "metodo": "la salida se eligió "
                                                                      "después de ver los datos", "valor": 3}},
                         fragil=True, fragil_motivos=["la salida se eligió después de ver los datos"]))
    assert e2["robustez"]["corregida"] == {"tipo": "sin_p", "metodo": "la salida se eligió después de ver los datos",
                                           "valor": None, "pasa": False}
    assert e2["fragil_motivos"] == ["sin corrección por número de pruebas", "la salida se eligió después de ver los datos"]
    tipo = (lambda m, **kw: normalizar(fila("t", robustez={"corregida": {"metodo": m, "valor": 0.5, "pasa": False,
                                                                         **kw}}))["robustez"]["corregida"]["tipo"])
    assert tipo("Holm entre las 23 configuraciones del estudio (p frente a la base emparejada)") == "holm"
    assert tipo("Benjamini-Hochberg (q) entre las 2.000 del laboratorio") == "bh" and tipo("q") == "bh"
    assert tipo("Sharpe deflactado del laboratorio vivo") == "dsr" and tipo("p") == "p" and tipo("otra cosa") is None
    assert tipo("Holm", tipo="bh") == "bh" and tipo("Holm", tipo="raro") == "holm"
    pasa = normalizar(fila("ok", probadas=23, robustez={"corregida": {"tipo": "holm", "metodo": "Holm", "valor": 0.01,
                                                                      "pasa": True}}))
    assert not pasa["fragil"] and pasa["robustez"]["corregida"]["tipo"] == "holm"
    assert f["perfil_corto"] is None and normalizar(fila("c", perfil_costes="laboratorio"))["perfil_corto"] == \
        "costes del laboratorio (sin funding)"
    assert "sin funding" in normalizar(fila("c", perfil_costes="laboratorio"))["perfil_texto"]


def test_orden_por_pct_dia_con_desempates_y_fuera_las_que_pierden():
    filas = [normalizar(x) for x in (
        fila("a", R=0.1, op=1.0), fila("b", R=0.2, op=0.5), fila("c", R=0.05, op=2.0, n=300),
        fila("d", R=0.3, op=1.0), fila("e", R=-0.01, op=5.0), fila("f", R=0.0, op=3.0), fila("g", R=None))]
    out = ordenar(filas)
    # a, b y c dan 0,05 %/jornada: manda op_dia (c 2,0 > a 1,0 > b 0,5); el R = 0 entra; e pierde y g no tiene R
    assert [(f["id"], f["posicion"]) for f in out] == [("d", 1), ("c", 2), ("a", 3), ("b", 4), ("f", 5), ("e", None),
                                                       ("g", None)]
    h = [normalizar(fila("x", R=0.1, op=1.0, n=50)), normalizar(fila("w", R=0.1, op=1.0, n=90))]
    assert [f["id"] for f in ordenar(h)] == ["w", "x"]   # mismo % y mismas operaciones al día: más operaciones fuera


def test_carpeta_se_recarga_por_mtime_y_tolera_lo_roto(tmp_path):
    car = tmp_path / "ranking"
    t0 = time.time() - 100
    poner(car, "uno.json", fila("uno", R=0.1), t0)
    poner(car, "varias.json", {"formato": 1, "filas": [fila("dos", R=0.2), fila("tres", R=-0.2), {"sin": "id"}]}, t0)
    poner(car, "roto.json", "{no es json", t0)
    (car / "roto.json").write_text("{no es json", encoding="utf-8")
    poner(car, "_importado.json", {"no": "es una fila"}, t0)
    poner(car, "vacio.json", {"formato": 1}, t0)
    r = rk(tmp_path)
    res = r.calcular()
    assert {f["id"] for f in res["filas"]} == {"uno", "dos", "tres"} and res["n_ranking"] == 2
    est = res["fuentes"]["estandar"]
    assert est["archivos"] == 4 and est["filas"] == 3 and len(est["errores"]) == 2   # roto y sin filas
    assert any("roto.json" in e for e in est["errores"]) and any("vacio.json" in e for e in est["errores"])
    poner(car, "uno.json", fila("uno", R=-0.5), t0 + 10)     # cambia: se vuelve a leer
    os.remove(car / "varias.json")                           # se borra: sus filas se van
    res = r.calcular()
    assert {f["id"]: f["posicion"] for f in res["filas"]} == {"uno": None}
    poner(car, "otro.json", fila("uno", R=0.7), t0 + 20)     # el mismo id en un archivo más nuevo: manda ese
    res = r.calcular()
    assert res["filas"][0]["fuera"]["R"] == 0.7 and res["fuentes"]["estandar"]["duplicadas"] == 1
    sin = rk(tmp_path / "no_hay").calcular()
    assert sin["n_total"] == 0 and sin["fuentes"]["estandar"]["estado"] == "sin carpeta"


def _json_estricto(txt: str):
    """Como el JSON.parse del navegador: NaN e Infinity no valen."""
    def no(c):
        raise ValueError(f"{c} no es JSON para el navegador")
    return json.loads(txt, parse_constant=no)


def _rotas(tmp_path: Path) -> None:
    """Archivos rotos de verdad (los de la revisión): ninguno puede tumbar el ranking."""
    car, lab = tmp_path / "ranking", tmp_path / "lab_vivo"
    t0 = time.time() - 100
    poner(car, "buena.json", fila("buena", R=0.2), t0)
    poner(car, "motivos.json", {**fila("motivos", R=0.1), "fragil": True, "fragil_motivos": 5}, t0)
    poner(car, "perfil.json", {**fila("perfil", R=0.1), "perfil_costes": ["a"]}, t0)
    poner(car, "estado.json", {**fila("estado", R=0.1), "estado": ["a"], "probadas": 10 ** 400}, t0)
    poner(car, "vivo.json", {**fila("vivo", R=0.1), "en_vivo": {"n": 1, "R": float("nan"), "desde": float("inf"),
                                                                 "lista": [1, 2], "texto": "x" * 500,
                                                                 "avance": {"R": float("-inf"), "n": 2, "mas": {"a": 1}}},
                                 "fuera": {"n": 10, "R": 0.1, "jornadas": 100, "desde": 20250101}}, t0)
    poner(car, "enorme.json", {"id": "enorme", "fuera": {"n": 10 ** 400, "R": 10 ** 400}}, t0)
    (car / "anidado.json").write_text("[" * 20000 + "]" * 20000, encoding="utf-8")   # RecursionError al leerlo
    os.utime(car / "anidado.json", (t0, t0))
    poner(car, "revienta.json", {"filas": [fila("revienta"), fila("otra_buena", R=0.05)]}, t0)
    h = "h-20261029-1500-prueba_x"
    tarjeta = (lambda v, **kw: {"id": f"{h}:{v}", "hipotesis": h, "titulo": "T", "fuera": {"n": 50, "R": 0.1},
                                "op_dia": 1.0, "cuenta": {"pct_dia": 0.05}, **kw})
    poner(lab, "mejores.json", {"ts": 5, "mejores": [
        tarjeta("V1", hipotesis=123), tarjeta("V2", clase="descartada_completa", motivos=5), tarjeta("V3", op_dia=1e-12),
        tarjeta("V4", op_dia=1e-320, cuenta=[1, 2]), tarjeta("V5", avance={"desde": 1, "faltan": "x", "R_medio": float("nan")}),
        tarjeta("V6", clase={"a": 1}), tarjeta("REVIENTA"), "no es una tarjeta", 7]}, t0)
    poner(lab, "estado.json", {"ts": 1, "latido": 1, "estado": "activo", "contadores": [1], "mejores": {"no": "lista"},
                               "en_avance": 5, "datos": {"periodo": {"corte": "2025-08-16", "hasta": "9999-12-31"}}}, t0)


def test_filas_y_tarjetas_rotas_no_tumban_el_ranking_ni_dejan_nan(tmp_path, monkeypatch):
    """Una fila o una tarjeta mal formada se salta (o se limpia) y se apunta en los errores de su fuente: el ranking
    sale igual, con las buenas, y su JSON (respuesta y mensaje del bus) nunca lleva NaN ni Infinity."""
    _rotas(tmp_path)
    norm, tarj = ranking.normalizar, ranking.de_tarjeta

    def normalizar_que_revienta(x, archivo=None):
        if isinstance(x, dict) and x.get("id") == "revienta":
            raise TypeError("fallo inesperado")
        return norm(x, archivo)

    def tarjeta_que_revienta(t, *a, **kw):
        if isinstance(t, dict) and str(t.get("id")).endswith(":REVIENTA"):
            raise OverflowError("fallo inesperado")
        return tarj(t, *a, **kw)

    monkeypatch.setattr(ranking, "normalizar", normalizar_que_revienta)
    monkeypatch.setattr(ranking, "de_tarjeta", tarjeta_que_revienta)
    (tmp_path / "ranking_estado.json").write_text('{"vistas": [1], "actual": {"a": NaN}, "anterior": 5, "cambio": NaN}',
                                                  encoding="utf-8")
    (tmp_path / "estrategia_activa.json").write_text('{"preset": ["ruptura3"], "nombre": 5}', encoding="utf-8")
    store = Store(tmp_path / "t.db")
    bus = Bus(store)
    r = rk(tmp_path, bus=bus)
    res = r.calcular({})
    por = {f["id"]: f for f in res["filas"]}
    assert {"buena", "motivos", "perfil", "estado", "vivo", "otra_buena"} <= set(por) and "revienta" not in por
    assert por["motivos"]["fragil_motivos"] == ["marcada frágil por su estudio"] and por["perfil"]["perfil_costes"] is None
    assert por["estado"]["estado"] == "backtest" and por["estado"]["probadas"] is None
    assert por["vivo"]["en_vivo"] == {"n": 1, "texto": "x" * 200, "avance": {"n": 2}}
    assert por["vivo"]["fuera"].get("desde") is None and por["enorme"]["fuera"] is None
    est = res["fuentes"]["estandar"]
    assert any(e.startswith("anidado.json: no se puede leer (RecursionError") for e in est["errores"])
    assert any(e.startswith("revienta.json: 1 fila no vale: fila 1 (TypeError") for e in est["errores"])
    lab = {f["id"].split(":")[-1]: f for f in res["filas"] if f["origen"] == "laboratorio_vivo"}
    assert set(lab) == {"V1", "V2", "V3", "V4", "V5", "V6"} and len(res["fuentes"]["lab_vivo"]["errores"]) == 1
    assert "REVIENTA" in res["fuentes"]["lab_vivo"]["errores"][0]
    assert lab["V2"]["fragil_motivos"] == ["el laboratorio vivo la dejó como descartada completa"]
    for f in lab.values():   # el periodo roto (9999-12-31) se queda en hoy: días y jornadas con sentido
        assert f["fuera"]["dias"] is None or f["fuera"]["dias"] <= ranking.DIAS_MAX
        assert f["fuera"]["hasta"] is None or f["fuera"]["hasta"] <= time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400))
    assert res["en_marcha"]["id"] is None   # el preset de estrategia_activa.json no era un texto
    _json_estricto(json.dumps(res, ensure_ascii=False))   # sin NaN ni Infinity en lo que sale
    assert r.publicar(res) and _json_estricto(json.dumps(bus.state["ranking"], ensure_ascii=False))
    store.close()


async def test_api_ranking_con_archivos_rotos_y_su_etag(tmp_path, monkeypatch):
    """GET /api/ranking con archivos rotos responde 200 con JSON válido para el navegador. Lleva ETag: con
    If-None-Match igual, 304 sin cuerpo; si cambia algo, 200 con otro ETag."""
    _rotas(tmp_path)
    store = Store(tmp_path / "t.db")
    bus = Bus(store)
    sv = Services(ranking=rk(tmp_path, bus=bus))
    async with TestClient(TestServer(make_app(bus, lambda: {}, sv))) as client:
        r1 = await client.get("/api/ranking?limite=5000")
        assert r1.status == 200 and "buena" in [f["id"] for f in _json_estricto(await r1.text())["filas"]]
        etag = r1.headers["ETag"]
        assert etag.startswith('"') and r1.headers["Cache-Control"] == "no-cache"
        await asyncio.sleep(ranking.CACHE_S + 0.1)   # otro cálculo (otra hora): el mismo contenido, el mismo ETag
        r2 = await client.get("/api/ranking?limite=5000", headers={"If-None-Match": etag})
        assert r2.status == 304 and await r2.read() == b"" and r2.headers["ETag"] == etag
        r3 = await client.get("/api/ranking?limite=1", headers={"If-None-Match": etag})   # otra consulta, otro contenido
        assert r3.status == 200 and r3.headers["ETag"] != etag
        poner(tmp_path / "ranking", "nueva.json", fila("nueva", R=0.9))
        await asyncio.sleep(ranking.CACHE_S + 0.1)
        r4 = await client.get("/api/ranking?limite=5000", headers={"If-None-Match": f'"x", {etag}'})
        assert r4.status == 200 and r4.headers["ETag"] != etag
        assert _json_estricto(await r4.text())["filas"][0]["id"] == "nueva"
    store.close()


def test_en_marcha_con_su_puesto_o_fuera_del_ranking(tmp_path):
    car = tmp_path / "ranking"
    poner(car, "a.json", fila("a", R=0.3))
    poner(car, "ruptura3.json", fila("ruptura3", R=0.2, op=0.4))
    poner(car, "seis2.json", fila("seis2", R=-0.075, op=4.8))
    r = rk(tmp_path)
    em = r.calcular({"openings": openings("ruptura3", "Ruptura V3")})["en_marcha"]
    assert em["id"] == "ruptura3" and em["posicion"] == 2 and em["en_ranking"] and em["texto"] == "Puesto 2 de 2 en el ranking"
    assert em["fila"]["estado"] == "en_paper" and em["fila"]["en_marcha"] and em["desde"] == 1_793_276_347_112
    em = r.calcular({"openings": openings("seis2")})["en_marcha"]
    assert em["posicion"] is None and not em["en_ranking"]
    assert em["texto"] == "Fuera del ranking: pierde fuera de muestra (−0,075 R por operación)"
    res = r.calcular({"openings": openings("seis", "6 diarias")})   # sin su fila: una de en vivo, sin medida
    assert res["en_marcha"]["texto"] == "Fuera del ranking: sin medida fuera de muestra"
    assert next(f for f in res["filas"] if f["id"] == "seis")["origen"] == "en_vivo"
    # no pierde, pero no se puede poner en su puesto: no se dice «pierde»
    poner(car, "seis.json", {"id": "seis", "fuera": {"n": 40, "R": 0.12}}, time.time() + 5)
    em = r.calcular({"openings": openings("seis", "6 diarias")})["en_marcha"]
    assert em["texto"] == ("Fuera del ranking: no pierde (+0,120 R por operación), pero le faltan las operaciones por "
                           "jornada para calcular su % de la cuenta al día") and "pierde fuera" not in em["texto"]
    poner(car, "seis.json", {"id": "seis", "fuera": {"n": 0, "R": 0.0}, "op_dia": 0.0}, time.time() + 10)
    em = r.calcular({"openings": openings("seis", "6 diarias")})["en_marcha"]
    assert em["texto"] == "Fuera del ranking: no pierde (+0,000 R por operación), pero no tiene operaciones fuera de muestra"
    os.remove(car / "seis.json")
    res = r.calcular({"openings": openings(None, "Cambiada por la evolución")})
    assert res["en_marcha"]["id"] is None and "evolución" in res["en_marcha"]["texto"]
    assert "no ha publicado" in r.calcular({})["en_marcha"]["texto"]
    (tmp_path / "estrategia_activa.json").write_text(json.dumps({"preset": "ruptura3", "nombre": "Ruptura V3"}),
                                                     encoding="utf-8")   # sin el estado del motor, el archivo
    em = rk(tmp_path).calcular({})["en_marcha"]
    assert em["id"] == "ruptura3" and em["fuente"] == "estrategia_activa.json" and em["posicion"] == 2


def test_en_vivo_del_paper_por_preset_y_de_la_prueba_en_avance(tmp_path):
    car = tmp_path / "ranking"
    poner(car, "seis.json", fila("seis", R=-0.12))
    poner(car, "e2.json", fila("e2_impulso_7d", R=0.37, estado="descartada_por_el_usuario"))
    lab_id = "btc:ma20|rvol:1,5|todos|ninguno|asia|SOL|todos|pb:15|3|2|sig"
    poner(car, "lab.json", fila(f"lab2000:{lab_id}", R=0.2, origen="laboratorio_2000"))
    trades = [{"strategy": "impulso-corto-sin-noticia", "tags": {"preset": "seis"}, "R": 1.5, "pnl": 0.8, "bruto": 0.9,
               "opened": 10, "closed": 20},
              {"strategy": "impulso-largo-sin-noticia", "tags": {"preset": "seis"}, "R": -1.1, "pnl": -0.5, "opened": 5,
               "closed": 30},
              {"strategy": "impulso-ruptura3-largo-sin-noticia", "tags": {"modelo": "ruptura3"}, "R": 2.0, "pnl": 1.0},
              {"strategy": "noticia-fiable-largo", "tags": {"origen": "noticia_fiable"}, "R": -1.0, "pnl": -0.4},
              {"strategy": "impulso-largo-noticia-a-favor", "tags": {"origen": "motor_aperturas"}, "R": 0.5, "pnl": 0.2},
              {"strategy": "fase2", "R": 0.1, "pnl": 0.05}]
    vig = {"ts": 1, "estado": "ok", "estrategias": [
        {"id": lab_id, "codigo": "E1", "nombre": "E1", "desde": 100, "vivo": {"operaciones": 2, "R_medio": 0.5,
                                                                             "ganadoras": 0.5, "abiertas": 1}},
        {"id": "ap_mom|7d", "codigo": "E2-7d", "nombre": "E2 7d", "desde": 100, "vivo": {"operaciones": 1, "R_medio": -1}},
        {"id": "btc|7d", "codigo": "E1-7d", "nombre": "E1 con 7 días", "desde": 100, "vivo": {"operaciones": 0},
         "backtest": {"operaciones": 30, "R_medio": 0.1, "ganadoras": 0.4, "periodo": "2025-08-16 a 2026-10-23"},
         "reglas": {"sl_pct": 3.0, "r": 2.0, "neto_pct": 5.76}}]}
    r = rk(tmp_path, get_resultados=lambda: {"trades": trades}, get_vigilancia=lambda: vig,
           get_openings=lambda: openings("ruptura3"),
           get_cartera=lambda: {"positions": [{"tags": {"preset": "seis"}, "opened": 1}]})
    res = r.calcular(r.foto_vivo())
    por = {f["id"]: f for f in res["filas"]}
    ev = por["seis"]["en_vivo"]
    assert (ev["fuente"], ev["n"], ev["R"], ev["gana_pct"], ev["neto_usdc"], ev["bruto_usdc"], ev["desde"], ev["hasta"],
            ev["abiertas"]) == ("paper", 2, 0.2, 50.0, 0.3, 0.4, 1, 30, 1)
    assert por["seis"]["estado"] == "backtest" and por["ruptura3"]["estado"] == "en_paper"   # ruptura3: la que opera
    assert por["ruptura3"]["en_vivo"]["n"] == 1 and por["ruptura3"]["origen"] == "en_vivo"
    assert por["en_vivo:noticia_fiable"]["en_vivo"]["R"] == -1.0 and por["en_vivo:motor"]["en_vivo"]["n"] == 1
    assert por["en_vivo:paper_otras"]["en_vivo"]["n"] == 1
    assert por[f"lab2000:{lab_id}"]["estado"] == "en_avance" and por[f"lab2000:{lab_id}"]["en_vivo"]["virtual"]
    assert por[f"lab2000:{lab_id}"]["en_vivo"]["gana_pct"] == 50.0
    assert por["e2_impulso_7d"]["estado"] == "descartada_por_el_usuario"   # el descarte del usuario manda
    assert por["e2_impulso_7d"]["en_vivo"]["fuente"] == "vigilancia"
    e1 = por["vigilancia:E1-7d"]   # sin estudio: su propia fila con el backtest de la vigilancia
    assert e1["estado"] == "en_avance" and e1["fuera"]["n"] == 30 and e1["op_dia"] == round(30 / 309, 4)
    assert e1["cumple_reglas"] is True and res["fuentes"]["paper"]["operaciones"] == 6
    assert res["fuentes"]["vigilancia"]["estrategias"] == 3


def test_tarjeta_del_laboratorio_vivo_en_jornadas(tmp_path):
    t = {"id": "h-20261029-1130-h06-keltner_1h:V3", "hipotesis": "h-20261029-1130-h06-keltner_1h", "titulo": "Keltner",
         "familia": "keltner", "regla": "sale del canal", "clase": "en_avance", "cumple_reglas": True,
         "fuera_de_reglas": False, "op_dia": 0.8, "dentro": {"n": 570, "R": -0.02},
         "fuera": {"n": 328, "R": 0.13, "p": 0.16, "q": 0.3, "dsr": 0.5, "mitades": [0.2, 0.03]},
         "cuenta": {"R_dia": 0.104, "pct_dia": 0.052, "caida_max_pct": 8.0},
         "robustez": {"sin_5_mayores": 0.05, "base_p": 0.006}, "motivos": [],
         "avance": {"desde": 1_793_000_000_000, "completas": 4, "abiertas": 1, "R_medio": 0.3, "p_holm": None}}
    per = {"desde": "2023-10-01", "hasta": "2026-09-30", "corte": "2025-08-16"}
    f = de_tarjeta(t, per, 1, 40, "lab_vivo/mejores.json")
    factor = 411 / 292   # del 16-08-2025 al 30-09-2026: 411 días naturales y 292 jornadas
    assert f["id"] == "lab_vivo:h-20261029-1130-h06-keltner_1h:V3" and f["nombre"] == "Keltner (V3)"
    assert f["origen"] == "laboratorio_vivo" and f["fecha"] == "2026-10-29" and f["perfil_costes"] == "okx_eu_con_xperps"
    assert f["op_dia"] == round(0.8 * factor, 4) and f["pct_dia"] == round(0.052 * factor, 4)
    assert f["op_dia_natural"] == 0.8 and f["fuera"]["jornadas"] == 292 and f["fuera"]["dias"] == 411
    assert f["robustez"]["corregida"] == {"tipo": "bh", "metodo": "Benjamini-Hochberg (q) del laboratorio vivo",
                                          "valor": 0.3, "pasa": False}
    assert f["estado"] == "en_avance" and f["en_vivo"]["virtual"] and f["en_vivo"]["n"] == 4 and f["probadas"] == 40
    assert f["fragil_motivos"] == ["gana fuera de muestra pero pierde dentro",
                                   "no pasa la corrección por número de pruebas"]
    sin_per = de_tarjeta(t)   # sin estado.json: los días salen de n / op_dia desde el corte
    assert sin_per["fuera"]["dias"] == 410 and sin_per["op_dia"] is not None
    assert de_tarjeta({**t, "clase": "descartada_usuario"})["estado"] == "descartada_por_el_usuario"
    desc = de_tarjeta({**t, "clase": "descartada_robustez", "motivos": ["B1: no bate a su base (p 0,21)"],
                       "dentro": {"n": 570, "R": 0.02}, "fuera": {**t["fuera"], "q": 0.05}}, per)
    assert desc["fragil_motivos"] == ["el laboratorio vivo la descartó (B1: no bate a su base (p 0,21))"]
    assert desc["estado"] == "backtest" and "no ha pasado: B1" in desc["nota"]
    assert de_tarjeta({**t, "clase": "revocada"}, per)["fragil_motivos"][-1] == "el laboratorio vivo la dejó como revocada"
    assert de_tarjeta({**t, "fuera_de_reglas": True})["cumple_reglas"] is False
    nada = de_tarjeta({**t, "fuera": None, "avance": None})
    assert nada["pct_dia"] is None and not entra(nada) and nada["en_vivo"] is None
    assert de_tarjeta({"titulo": "sin id"}) is None and de_tarjeta({"id": "  "}) is None
    for op in (1e-12, 1e-320):   # sin estado.json, un op_dia ínfimo no desborda: como mucho 10 años y hasta hoy
        x = de_tarjeta({**t, "op_dia": op})
        assert 0 < x["fuera"]["dias"] <= ranking.DIAS_MAX and x["fuera"]["jornadas"] > 0
    for per_roto in ({"corte": "2025-08-16", "hasta": "9999-12-31"}, {"corte": "9999-12-31", "hasta": "9999-12-31"},
                     {"corte": "0001-01-01", "hasta": "0001-01-02"}):
        x = de_tarjeta(t, per_roto)
        assert (x["fuera"].get("dias") or 0) <= ranking.DIAS_MAX
    raro = de_tarjeta({**t, "hipotesis": 123, "motivos": 5, "clase": ["x"], "titulo": {"a": 1}}, per, 10 ** 30)
    assert raro["fecha"] == "2026-10-29" and raro["nombre"] == "h-20261029-1130-h06-keltner_1h:V3 (V3)"
    assert raro["estado"] == "backtest" and raro["nota"] is None
    assert de_tarjeta({**t, "id": "otra:V1", "hipotesis": None}, per, 10 ** 30)["fecha"] is None   # ts imposible


def test_laboratorio_vivo_entra_solo_desde_sus_archivos(tmp_path):
    lab = tmp_path / "lab_vivo"
    lab.mkdir()
    t = {"id": "h-20261029-1130-h01-tsmom:V1", "titulo": "Momento", "familia": "momento_serie", "clase": "candidata",
         "cumple_reglas": True, "op_dia": 0.6, "dentro": {"n": 500, "R": 0.1}, "fuera": {"n": 245, "R": 0.15},
         "cuenta": {"R_dia": 0.09, "pct_dia": 0.045}}
    (lab / "mejores.json").write_text(json.dumps({"ts": 5, "mejores": [t]}), encoding="utf-8")
    res = rk(tmp_path).calcular()
    assert [f["id"] for f in res["filas"]] == ["lab_vivo:h-20261029-1130-h01-tsmom:V1"] and res["n_ranking"] == 1
    assert res["fuentes"]["lab_vivo"]["filas"] == 1 and res["fuentes"]["lab_vivo"]["estado"] == "apagado"


def test_nueva_y_puesto_de_antes_sobreviven_a_un_reinicio(tmp_path):
    car = tmp_path / "ranking"
    t0 = time.time() - 100
    poner(car, "a.json", fila("a", R=0.3), t0)
    poner(car, "b.json", fila("b", R=0.2), t0)
    reloj = {"t": 1_000_000_000_000}
    r = rk(tmp_path, clock=lambda: reloj["t"])
    res = r.calcular()
    assert [(f["id"], f["nueva"], f["posicion_antes"], f["entra_desde"]) for f in res["filas"]] == [
        ("a", False, None, None), ("b", False, None, None)]   # lo que ya estaba al empezar no es nuevo
    reloj["t"] += 3600_000
    poner(car, "c.json", fila("c", R=0.5), t0 + 10)
    res = r.calcular()
    assert [(f["id"], f["posicion"], f["nueva"], f["posicion_antes"]) for f in res["filas"]] == [
        ("c", 1, True, None), ("a", 2, False, 1), ("b", 3, False, 2)]
    assert res["filas"][0]["entra_desde"] == reloj["t"]
    r2 = rk(tmp_path, clock=lambda: reloj["t"] + 3600_000)   # otro arranque, una hora después
    res = r2.calcular()
    assert [(f["id"], f["nueva"], f["posicion_antes"]) for f in res["filas"]] == [("c", True, None), ("a", False, 1),
                                                                                  ("b", False, 2)]
    r3 = rk(tmp_path, clock=lambda: reloj["t"] + 25 * 3600_000)   # pasadas 24 h ya no es nueva
    assert [f["nueva"] for f in r3.calcular()["filas"]] == [False, False, False]
    d = json.loads((tmp_path / "ranking_estado.json").read_text(encoding="utf-8"))
    assert d["actual"] == {"c": 1, "a": 2, "b": 3} and d["vistas"]["c"] == reloj["t"]


def test_consulta_filtros_limite_y_puestos_del_ranking_entero(tmp_path):
    car = tmp_path / "ranking"
    poner(car, "1.json", fila("lab", R=0.4, op=1.0, origen="laboratorio_2000", dentro=-0.1))
    poner(car, "2.json", fila("cur", R=0.3, op=0.2, origen="curso"))
    poner(car, "3.json", fila("sel", R=0.2, op=1.0, origen="selectiva", cumple_reglas=False))
    poner(car, "4.json", fila("pierde", R=-0.1, origen="curso"))
    r = rk(tmp_path)
    out = r.consulta({}, origen="curso,selectiva")
    assert [(f["id"], f["posicion"]) for f in out["filas"]] == [("sel", 2), ("cur", 3)]   # los puestos no cambian
    assert out["n_total"] == 4 and out["n_ranking"] == 3 and out["n_filtradas"] == 2
    assert [f["id"] for f in r.consulta({}, cumple=True)["filas"]] == ["lab", "cur"]
    assert [f["id"] for f in r.consulta({}, cumple=False)["filas"]] == ["sel"]
    assert [f["id"] for f in r.consulta({}, min_op_dia=0.5)["filas"]] == ["lab", "sel"]
    assert [f["id"] for f in r.consulta({}, fragil=False)["filas"]] == ["sel", "cur"]   # «lab» gana fuera y pierde dentro
    assert [f["id"] for f in r.consulta({}, limite=1)["filas"]] == ["lab"]
    todas = r.consulta({}, todas=True)
    assert [f["id"] for f in todas["filas"]] == ["lab", "sel", "cur", "pierde"] and todas["filas"][-1]["posicion"] is None
    assert {o["id"]: (o["n"], o["n_ranking"]) for o in out["origenes"]} == {"laboratorio_2000": (1, 1), "curso": (2, 1),
                                                                          "selectiva": (1, 1)}
    assert "suerte" in out["nota"] and out["criterio"]["riesgo_pct"] == 0.5


def test_mensaje_del_bus_con_las_20_primeras_y_solo_si_cambia(tmp_path):
    car = tmp_path / "ranking"
    t0 = time.time() - 100
    poner(car, "muchas.json", {"filas": [fila(f"f{i:02d}", R=0.01 * (i + 1)) for i in range(25)]}, t0)
    store = Store(tmp_path / "t.db")
    bus = Bus(store)
    r = rk(tmp_path, bus=bus)
    res = r.calcular()
    assert r.publicar(res) is True and not r.publicar(r.calcular())   # sin cambios no se repite
    m = bus.state["ranking"]
    assert m["type"] == "ranking" and len(m["filas"]) == 20 and m["filas"][0]["id"] == "f24"
    assert m["n_ranking"] == 25 and "perfiles" not in m["filas"][0] and m["en_marcha"]["posicion"] is None
    poner(car, "muchas.json", {"filas": [fila(f"f{i:02d}", R=0.01 * (25 - i)) for i in range(25)]}, t0 + 10)
    assert r.publicar(r.calcular()) is True and bus.state["ranking"]["filas"][0]["id"] == "f00"
    store.close()


async def test_run_publica_al_arrancar(tmp_path, monkeypatch):
    monkeypatch.setattr(ranking, "BUS_CADA_S", 0.05)
    poner(tmp_path / "ranking", "a.json", fila("a"))
    store = Store(tmp_path / "t.db")
    bus = Bus(store)
    r = rk(tmp_path, bus=bus)
    t = asyncio.create_task(r.run())
    try:
        for _ in range(100):
            if "ranking" in bus.state:
                break
            await asyncio.sleep(0.02)
        assert bus.state["ranking"]["filas"][0]["id"] == "a"
    finally:
        t.cancel()
    store.close()


async def test_api_ranking_filtros_y_400(tmp_path):
    car = tmp_path / "ranking"
    poner(car, "1.json", fila("lab", R=0.4, origen="laboratorio_2000"))
    poner(car, "2.json", fila("cur", R=0.3, op=0.2, origen="curso"))
    store = Store(tmp_path / "t.db")
    bus = Bus(store)
    bus.publish(openings("ruptura3", "Ruptura V3"))
    sv = Services(ranking=rk(tmp_path, bus=bus))
    async with TestClient(TestServer(make_app(bus, lambda: {}, sv))) as client:
        j = await (await client.get("/api/ranking")).json()
        assert [f["id"] for f in j["filas"]] == ["lab", "cur"] and j["n_total"] == 3 and j["n_ranking"] == 2
        assert j["en_marcha"]["id"] == "ruptura3" and j["en_marcha"]["texto"].startswith("Fuera del ranking")
        assert set(j) >= {"ts", "n_total", "n_ranking", "filas", "en_marcha", "fuentes", "nota"}
        j = await (await client.get("/api/ranking?origen=curso&cumple=1&min_op_dia=0,1&limite=5")).json()
        assert [f["id"] for f in j["filas"]] == ["cur"] and j["filas"][0]["posicion"] == 2
        j = await (await client.get("/api/ranking?todas=1&fragil=0")).json()
        assert [f["id"] for f in j["filas"]] == ["lab", "cur", "ruptura3"]
        for lim, esperado in (("0", 1), ("99999", 5000)):   # fuera de 1-5.000 se recorta (docs/04), no da 400
            r = await client.get(f"/api/ranking?limite={lim}")
            assert r.status == 200 and (await r.json())["filtros"]["limite"] == esperado
        for url in ("/api/ranking?origen=Curso", "/api/ranking?origen=a/b", "/api/ranking?min_op_dia=x",
                    "/api/ranking?min_op_dia=-1", "/api/ranking?cumple=quiza", "/api/ranking?limite=x",
                    "/api/ranking?fragil=2", "/api/ranking?min_op_dia=nan"):
            assert (await client.get(url)).status == 400, url
    store.close()


def test_en_directo_la_web_va_sin_los_ejemplos_incrustados():
    """Los ejemplos de la demostración (rankingData y labVivoData) no viajan en directo: allí llega todo por la API."""
    from bot import server
    html = config.DASHBOARD.read_text(encoding="utf-8")
    assert 'id="rankingData"' in html and 'id="labVivoData"' in html
    live = server.BT_BLOCK.sub("<!--BT-->", html)
    assert 'id="rankingData"' not in live and 'id="labVivoData"' not in live and "function demoRanking" in live
    assert len(html) - len(live) > 25_000


def test_el_ejemplo_de_la_web_lleva_las_filas_reales_como_las_ensena_el_bot():
    """Las filas que no empiezan por «Ejemplo» en rankingData son filas del bot (normalizar no las cambia): con su
    perfil de costes, su corrección (con su tipo) y su fragilidad; E2 con 7 días, frágil y descartada por ti."""
    html = config.DASHBOARD.read_text(encoding="utf-8")
    d = json.loads(html.split('<script type="application/json" id="rankingData">', 1)[1].split("</script>", 1)[0])
    reales = {f["id"]: f for f in d["filas"] if not f["nombre"].startswith("Ejemplo")}
    assert set(reales) == {"ruptura3", "e2_impulso_7d", "seis2", "seis",
                           "lab2000:ap_mom:15:0,2|rvol:1,5|todos|a_favor|todas|todas|todos|t:30|3|3|sig",
                           "lab2000:btc:ma20|rvol:1,5|todos|ninguno|asia|SOL|todos|pb:15|3|2|sig"}
    for f in d["filas"]:
        n = {k: v for k, v in normalizar(f).items() if v is not None}
        assert n == f, f["id"]   # ya normalizada: lo mismo que enseña el bot
        assert f["perfil_costes"] in ranking.PERFILES_TEXTO and f.get("perfil_corto")
    e2 = reales["e2_impulso_7d"]
    assert e2["fragil"] and e2["estado"] == "descartada_por_el_usuario" and e2["robustez"]["corregida"]["tipo"] == "sin_p"
    r3 = reales["ruptura3"]
    assert r3["robustez"]["corregida"]["tipo"] == "holm" and r3["robustez"]["corregida"]["valor"] == 1.0
    assert r3["perfil_costes"] == "backtest" and r3["fragil"]
    assert {f["perfil_costes"] for f in reales.values()} == {"backtest", "laboratorio"}
    assert d["ejemplo"] is True and d["criterio"] == ranking.CRITERIO   # al día: scripts/ranking_importar.py --demo


async def test_api_ranking_sin_el_modulo_lee_la_carpeta_por_defecto(tmp_path, monkeypatch):
    """Sin Services.ranking, el servidor crea el suyo en BACKTEST_DIR/ranking (y su estado en DATA_DIR)."""
    monkeypatch.setattr(config, "BACKTEST_DIR", tmp_path / "bt")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "datos")
    monkeypatch.delenv("BOT_LAB_VIVO_DIR", raising=False)
    store = Store(tmp_path / "t.db")
    bus = Bus(store)
    async with TestClient(TestServer(make_app(bus, lambda: {}))) as client:
        j = await (await client.get("/api/ranking")).json()
        assert j["n_total"] == 0 and j["filas"] == [] and j["fuentes"]["estandar"]["estado"] == "sin carpeta"
        poner(tmp_path / "bt" / "ranking", "a.json", fila("a"))
        await asyncio.sleep(ranking.CACHE_S + 0.1)
        j = await (await client.get("/api/ranking")).json()
        assert [f["id"] for f in j["filas"]] == ["a"]
    assert (tmp_path / "datos" / "ranking_estado.json").exists()
    store.close()


def test_seis4r3_en_marcha_es_su_fila_de_salidas_motor():
    """«6 diarias 4R · stop 3 %» (preset seis4r3) es la salida «F0 stop 3 % · objetivo 4R · tope 7d» de salidas_motor.py:
    en marcha se marca esa fila, como con seis4r."""
    assert ranking.SEIS4R3_FILA == "salidas_motor:F0 stop 3 % · objetivo 4R · tope 7d"
    assert ranking.PRESET_FILA["seis4r3"] == ranking.SEIS4R3_FILA != ranking.SEIS4R_FILA


def test_seis4r_en_marcha_es_su_fila_de_salidas_motor_sin_repetirla(tmp_path):
    """«6 diarias 4R» (preset seis4r) es la salida «F0 stop 2 % · objetivo 4R · tope 7d» de salidas_motor.py sobre las
    entradas de «6 diarias» (su réplica da las mismas operaciones): el ranking marca en marcha esa fila, la n.º 1, con
    sus operaciones del paper, sin crear otra «seis4r» repetida ni una de solo en vivo."""
    from bot import apertura as ap
    car = tmp_path / "ranking"
    fid = ranking.SEIS4R_FILA
    assert fid == "salidas_motor:F0 stop 2 % · objetivo 4R · tope 7d" and ranking.PRESET_FILA["seis4r"] == fid
    assert set(ranking.PRESET_FILA) == set(ap.PRESETS)   # cada preset del motor tiene su fila
    poner(car, "s4r.json", fila(fid, R=0.1228, n=757, op=2.474, dentro=-0.0413, origen="salidas",
                                estudio="salidas_motor", probadas=378))
    poner(car, "rup3.json", fila("ruptura3", R=0.4285, n=118, op=0.386, dentro=-0.033))
    poner(car, "seis.json", fila("seis", R=-0.119, op=4.8))
    trades = [{"strategy": "impulso-corto-sin-noticia", "tags": {"preset": "seis4r", "origen": "motor_aperturas"},
               "R": 3.9, "pnl": 1.9, "bruto": 2.0, "opened": 10, "closed": 20},
              {"strategy": "impulso-ruptura3-largo-sin-noticia", "tags": {"preset": "ruptura3", "modelo": "ruptura3"},
               "R": -1.0, "pnl": -0.5, "opened": 1, "closed": 5}]
    r = rk(tmp_path, get_resultados=lambda: {"trades": trades},
           get_openings=lambda: openings("seis4r", ap.PRESETS["seis4r"]["nombre"]),
           get_cartera=lambda: {"positions": [{"tags": {"preset": "seis4r"}, "opened": 30}]})
    res = r.calcular(r.foto_vivo())
    ids = [f["id"] for f in res["filas"]]
    assert sorted(ids) == sorted([fid, "ruptura3", "seis"]) and res["n_total"] == 3   # ni «seis4r» ni otra de en vivo
    em = res["en_marcha"]
    assert em["id"] == fid and em["preset"] == "seis4r" and em["posicion"] == 1 and em["en_ranking"]
    assert em["texto"] == "Puesto 1 de 2 en el ranking" and em["fila"]["en_marcha"] and em["fila"]["estado"] == "en_paper"
    por = {f["id"]: f for f in res["filas"]}
    f = por[fid]
    assert f["posicion"] == 1 and f["origen"] == "salidas" and f["fragil"] and f["pct_dia"] == pytest.approx(0.1521, abs=1e-3)
    assert (f["en_vivo"]["n"], f["en_vivo"]["R"], f["en_vivo"]["abiertas"], f["en_vivo"]["neto_usdc"]) == (1, 3.9, 1, 1.9)
    r3 = por["ruptura3"]
    assert not r3["en_marcha"] and r3["estado"] == "backtest" and r3["posicion"] == 2 and r3["en_vivo"]["n"] == 1
    assert [x["id"] for x in res["filas"] if x["en_marcha"]] == [fid]
    # sin el estado del motor, lo dice estrategia_activa.json (scripts/activar_estrategia.py --preset seis4r)
    (tmp_path / "estrategia_activa.json").write_text(json.dumps({"preset": "seis4r", "nombre": "6 diarias 4R"}),
                                                     encoding="utf-8")
    em2 = rk(tmp_path).calcular({})["en_marcha"]
    assert em2["id"] == fid and em2["fuente"] == "estrategia_activa.json" and em2["posicion"] == 1
