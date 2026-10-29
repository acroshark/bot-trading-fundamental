"""Lectura del laboratorio vivo desde el servidor (bot/lab_vivo_lectura.py; docs/07, apartado 14): solo lee archivos,
tolera que falten, «parado» con el latido viejo, el mensaje del bus recortado, el mando de pausa con el origen estricto y
las rutas GET /api/lab_vivo… Sin red y sin leer M:\\BotFundamental-datos (todo en tmp_path)."""
import asyncio
import json
import os
import re
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot import config
from bot.bus import Bus
from bot.lab_vivo_lectura import LabVivoLectura, dia_madrid
from bot.models import now_ms
from bot.server import Services, make_app
from bot.store import Store

ROOT = Path(__file__).resolve().parents[1]
HID = "h-20261029-1130-h06-keltner_1h"


def tarjeta(vid: str, R: float = 0.1, curva: bool = True) -> dict:
    t = {"id": vid, "hipotesis": vid.split(":")[0], "titulo": "Keltner 1 h", "familia": "keltner", "regla": "sale del canal",
         "clase": "candidata", "cumple_reglas": True, "fuera_de_reglas": False, "op_dia": 0.8,
         "dentro": {"n": 500, "R": 0.05}, "fuera": {"n": 328, "R": R, "p": 0.1, "q": 0.2, "mitades": [0.1, 0.2]},
         "cuenta": {"R_dia": 0.8 * R, "pct_dia": 0.4 * R, "caida_max_pct": 9.0}, "checks": {"R1": True}}
    if curva:
        t["avance"] = {"desde": 1, "completas": 3, "abiertas": 1, "R_medio": 0.2, "curva": [[0, 0.0], [1, 0.5]]}
    return t


def escribir(ruta: Path, d) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


def preparar(dir: Path, latido: int | None = None, n_mejores: int = 25) -> None:
    now = now_ms() if latido is None else latido
    est = {"formato": 1, "ts": now, "latido": now, "estado": "activo", "motivo": None, "aviso": "todo es virtual",
           "proxima_pausa": {"ventana": "europa"}, "recursos": {"procesos": 1, "memoria_libre_mb": 2048, "cpus": [3]},
           "datos": {"periodo": {"desde": "2023-10-01", "hasta": "2026-09-30", "corte": "2025-08-16"}},
           "codigo": {"canario": {"estado": "ok"}}, "contadores": {"n": {"total": 40}}, "embudo": [{"etapa": "x", "n": 1}],
           "meta": {"texto": "le faltan"}, "mejores": [tarjeta(f"{HID}:V{i}") for i in range(8)],
           "en_avance": [tarjeta(f"{HID}:A{i}") for i in range(7)], "listas": []}
    escribir(dir / "estado.json", est)
    escribir(dir / "mejores.json", {"formato": 1, "ts": now, "mejores": [tarjeta(f"{HID}:M{i}") for i in range(n_mejores)]})
    escribir(dir / "diario" / f"{dia_madrid(now_ms())}.json", {"formato": 1, "dia": dia_madrid(now_ms()), "texto": ["hoy"]})
    escribir(dir / "hipotesis.json", {"formato": 1, "ts": now, "hipotesis": [
        {"id": HID, "familia": "keltner", "estado": "admitida"},
        {"id": "h-20261029-1130-h07-otra", "familia": "squeeze", "estado": "rechazada"},
        {"id": "h-20261029-1130-h08-mas", "familia": "keltner", "estado": "rechazada"}]})
    escribir(dir / "hipotesis" / f"{HID}.json", {"formato": 1, "ts": now + 5, "variantes": [
        {"id": f"{HID}:V1", "tarjeta": tarjeta(f"{HID}:V1", R=0.3), "pruebas": [{"etapa": "rapida"}],
         "operaciones": {"fuera": [{"R": 1.0}] * 200}}]})
    escribir(dir / "cola.json", {"formato": 1, "pendientes": 4, "por_tipo": {"rapida": 4}})
    for i, n in enumerate(("2026-10-29-0715.json", "2026-10-29-1215.json", "2026-10-28-2230.json")):
        escribir(dir / "generador" / n, {"formato": 1, "ts": i, "resumen": n})


class _Bus:
    def __init__(self):
        self.msgs = []
        self.state = {}

    def publish(self, m):
        self.msgs.append(m)
        self.state[m["type"]] = m
        return m


def test_sin_archivos_todo_da_apagado_sin_errores(tmp_path):
    lab = LabVivoLectura(dir=tmp_path / "no_existe")
    assert lab.vista() == {"estado": "apagado", "nota": "El laboratorio vivo no está en marcha (scripts/lab-vivo.ps1)",
                           "pausa_manual": False}
    assert lab.resumen()["estado"] == "apagado" and lab.resumen()["type"] == "lab_vivo"
    assert lab.cola()["estado"] == "apagado" and lab.hipotesis()["hipotesis"] == []
    assert lab.diario() is None and lab.diario("2026-10-29") is None and lab.hipotesis_detalle(HID) is None
    assert lab.generador()["notas"] == [] and lab.tarjetas()["tarjetas"] == []
    assert lab.tarjetas()["estado"] == "apagado"
    assert lab.mando("pausar")["ok"] is False   # sin la carpeta no se crea nada
    assert not (tmp_path / "no_existe").exists()


def test_vista_con_mejores_diario_y_parado_con_el_latido_viejo(tmp_path):
    preparar(tmp_path)
    lab = LabVivoLectura(dir=tmp_path)
    v = lab.vista()
    assert v["estado"] == "activo" and len(v["mejores"]) == 20 and v["mejores"][0]["id"].endswith(":M0")
    assert v["diario"]["texto"] == ["hoy"] and v["pausa_manual"] is False
    viejo = now_ms() - 4 * 60_000
    preparar(tmp_path, latido=viejo)
    v = LabVivoLectura(dir=tmp_path).vista()
    assert v["estado"] == "parado" and v["estado_corredor"] == "activo" and "más de 3 min" in v["nota"]
    assert len(v["mejores"]) == 20   # el resto, lo último que hubo
    os.remove(tmp_path / "mejores.json")   # sin mejores.json, las de estado.json
    assert len(LabVivoLectura(dir=tmp_path).vista()["mejores"]) == 8


def _json_estricto(txt: str):
    """Como el JSON.parse del navegador: NaN e Infinity no valen."""
    def no(c):
        raise ValueError(f"{c} no es JSON para el navegador")
    return json.loads(txt, parse_constant=no)


def test_archivos_con_tipos_raros_no_rompen_nada(tmp_path):
    """estado.json con «mejores» como objeto, contadores como lista o listas que son números, y mejores.json roto: la
    vista, el resumen del bus y las tarjetas del ranking salen igual (sin lo que no vale)."""
    now = now_ms()
    escribir(tmp_path / "estado.json", {"ts": now, "latido": now, "estado": "activo", "mejores": {"a": 1}, "en_avance": 5,
                                        "listas": "x", "contadores": [1, 2], "codigo": [3], "recursos": 7,
                                        "datos": {"periodo": [1]}})
    escribir(tmp_path / "mejores.json", {"ts": now, "mejores": 5})
    lab = LabVivoLectura(dir=tmp_path)
    v = lab.vista()
    assert v["estado"] == "activo" and v["mejores"] == []
    escribir(tmp_path / "mejores.json", {"ts": now, "mejores": [tarjeta(f"{HID}:V1"), "x", 3, None]})
    assert [t["id"] for t in LabVivoLectura(dir=tmp_path).vista()["mejores"]] == [f"{HID}:V1"]   # solo las tarjetas
    r = lab.resumen()
    assert r["estado"] == "activo" and r["en_avance"] == [] and r["listas"] == [] and r["canario"] is None
    d = lab.tarjetas()
    assert [t["id"] for t, _, _ in d["tarjetas"]] == [f"{HID}:V1"] and d["probadas"] is None and d["periodo"] is None


async def test_nan_en_los_archivos_no_llega_al_navegador(tmp_path):
    """El laboratorio puede dejar NaN o Infinity en sus JSON (Python los escribe y los lee): ni GET /api/lab_vivo, ni el
    mensaje «lab_vivo» del bus, ni la foto inicial del WebSocket los llevan (el navegador no los lee y, en la foto, uno
    solo le haría perder todo el estado)."""
    preparar(tmp_path)
    est = json.loads((tmp_path / "estado.json").read_text(encoding="utf-8"))
    est["meta"] = {"mejor": {"R_dia": float("nan"), "pct_dia": float("inf")}, "falta_R_dia": float("-inf")}
    est["mejores"][0]["fuera"]["R"] = float("nan")
    escribir(tmp_path / "estado.json", est)
    assert "NaN" in (tmp_path / "estado.json").read_text(encoding="utf-8")
    store = Store(tmp_path / "t.db")
    bus = Bus(store)
    lab = LabVivoLectura(bus, dir=tmp_path)
    assert lab.publicar(forzar=True)
    sv = Services(lab_vivo=lab)
    async with TestClient(TestServer(make_app(bus, lambda: {}, sv))) as client:
        j = _json_estricto(await (await client.get("/api/lab_vivo")).text())
        assert j["meta"]["mejor"] == {"R_dia": None, "pct_dia": None} and j["meta"]["falta_R_dia"] is None
        ws = await client.ws_connect("/events")
        foto = _json_estricto((await ws.receive()).data)   # la foto entera, de una vez
        m = next(x for x in foto if x.get("type") == "lab_vivo")
        assert m["meta"]["mejor"]["R_dia"] is None and m["estado"] == "activo"
        bus.publish({"type": "prueba_nan", "x": [1.5, float("nan"), {"y": float("inf")}]})
        assert _json_estricto((await ws.receive()).data) == {"type": "prueba_nan", "x": [1.5, None, {"y": None}]}
        await ws.close()
    store.close()


def test_hipotesis_detalle_diario_cola_y_generador(tmp_path):
    preparar(tmp_path)
    lab = LabVivoLectura(dir=tmp_path)
    assert [h["id"] for h in lab.hipotesis(familia="keltner")["hipotesis"]] == [HID, "h-20261029-1130-h08-mas"]
    assert lab.hipotesis(estado="rechazada", limite=1) == {"hipotesis": [{"id": "h-20261029-1130-h07-otra",
                                                                          "familia": "squeeze", "estado": "rechazada"}],
                                                           "n": 2, "ts": lab.hipotesis()["ts"]}
    assert lab.hipotesis_detalle(HID)["variantes"][0]["id"] == f"{HID}:V1"
    assert lab.hipotesis_detalle("../estado") is None and lab.hipotesis_detalle("h-1-2-x") is None
    assert lab.diario("2026-01-01") is None and lab.diario("../x") is None
    assert lab.cola()["pendientes"] == 4
    g = lab.generador(limite=2)
    assert g["n"] == 3 and [x["archivo"] for x in g["notas"]] == ["2026-10-29-1215.json", "2026-10-29-0715.json"]


def test_resumen_del_bus_recorta_a_5_sin_curva_ni_checks(tmp_path):
    preparar(tmp_path)
    r = LabVivoLectura(dir=tmp_path).resumen()
    assert r["type"] == "lab_vivo" and r["estado"] == "activo" and r["canario"] == "ok"
    assert r["recursos"] == {"procesos": 1, "memoria_libre_mb": 2048}
    assert [len(r[k]) for k in ("mejores", "en_avance", "listas")] == [5, 5, 0]
    for t in r["mejores"] + r["en_avance"]:
        assert "checks" not in t and "curva" not in (t.get("avance") or {})
    assert set(r) >= {"ts", "motivo", "latido", "aviso", "proxima_pausa", "contadores", "embudo", "meta"}


def test_tarjetas_una_por_id_la_mas_reciente(tmp_path):
    preparar(tmp_path)
    d = LabVivoLectura(dir=tmp_path).tarjetas()
    ids = [t["id"] for t, _, _ in d["tarjetas"]]
    assert len(ids) == len(set(ids)) == 25 + 8 + 7
    v1 = next((t, a) for t, _, a in d["tarjetas"] if t["id"] == f"{HID}:V1")
    assert v1[0]["fuera"]["R"] == 0.3 and v1[1] == f"lab_vivo/hipotesis/{HID}.json"   # el detalle tiene el ts mayor
    assert "checks" not in v1[0] and "curva" not in v1[0]["avance"]
    assert d["periodo"]["hasta"] == "2026-09-30" and d["probadas"] == 40 and d["estado"] == "activo"


def test_en_memoria_no_se_guarda_el_detalle_entero_de_las_hipotesis(tmp_path):
    """Del detalle (hasta 200 operaciones por variante y hasta 1.000 hipótesis) solo quedan en la caché las tarjetas."""
    preparar(tmp_path)
    lab = LabVivoLectura(dir=tmp_path)
    lab.hipotesis()
    lab.tarjetas()
    assert str(tmp_path / "hipotesis.json") in lab.lector._c   # el índice no es de la carpeta hipotesis: se queda
    assert lab.hipotesis_detalle(HID)["variantes"][0]["operaciones"]["fuera"][0] == {"R": 1.0}
    detalle = [v for k, (_, v) in lab.lector._c.items() if f"{HID}.json" in k]
    assert len(detalle) == 1   # solo lo que sacó el ranking; la lectura del detalle para la API no se guarda
    guardado = json.dumps(detalle, ensure_ascii=False, default=str)
    assert "operaciones" not in guardado and '"pruebas"' not in guardado and "curva" not in guardado
    assert '"R": 0.3' in guardado
    os.remove(tmp_path / "hipotesis" / f"{HID}.json")
    assert all(f"{HID}.json" not in k for k in (lab.tarjetas() and lab.lector._c))   # el que se borra sale de la caché


def test_mando_pausar_y_reanudar_crea_y_borra_pausa_manual(tmp_path):
    preparar(tmp_path)
    lab = LabVivoLectura(dir=tmp_path)
    assert lab.mando("pausar") == {"ok": True, "why": None} and (tmp_path / "pausa_manual").exists()
    assert lab.mando("pausar")["why"] == "Ya estaba en pausa" and lab.vista()["pausa_manual"] is True
    assert lab.mando("reanudar") == {"ok": True, "why": None} and not (tmp_path / "pausa_manual").exists()
    assert lab.mando("reanudar")["why"] == "No estaba en pausa"
    assert lab.mando("criterios")["ok"] is False


async def test_run_publica_apagado_una_vez_y_luego_al_cambiar(tmp_path, monkeypatch):
    from bot import lab_vivo_lectura as lv
    monkeypatch.setattr(lv, "CADA_S", 0.02)
    bus = _Bus()
    lab = LabVivoLectura(bus, dir=tmp_path)
    t = asyncio.create_task(lab.run())
    try:
        await asyncio.sleep(0.15)
        assert [m["estado"] for m in bus.msgs] == ["apagado"]   # una sola vez
        preparar(tmp_path)
        for _ in range(100):
            if len(bus.msgs) > 1:
                break
            await asyncio.sleep(0.02)
        assert bus.msgs[-1]["estado"] == "activo" and len(bus.msgs[-1]["mejores"]) == 5
        n = len(bus.msgs)
        await asyncio.sleep(0.12)
        assert len(bus.msgs) == n   # sin cambios no publica
        lab.mando("pausar")
        for _ in range(100):
            if len(bus.msgs) > n:
                break
            await asyncio.sleep(0.02)
        assert bus.msgs[-1]["pausa_manual"] is True
    finally:
        t.cancel()


def test_el_servidor_no_importa_bot_lab_vivo():
    """El laboratorio vivo va en un proceso aparte: ni el servidor, ni la lectura, ni el ranking lo importan."""
    for f in ("bot/server.py", "bot/lab_vivo_lectura.py", "bot/ranking.py", "bot/__main__.py"):
        src = (ROOT / f).read_text(encoding="utf-8")
        assert not re.search(r"^\s*(from|import)\s+(\.|bot\.)lab_vivo(\s|\.|$)", src, re.M), f
        assert not re.search(r"^\s*import\s+sqlite3|^\s*from\s+sqlite3", src, re.M) or f == "bot/__main__.py", f
        assert "lab_vivo.db" not in src or f == "bot/lab_vivo_lectura.py", f   # solo lo nombra para decir que no la abre


async def test_rutas_get_del_laboratorio_vivo(tmp_path):
    preparar(tmp_path)
    store = Store(tmp_path / "t.db")
    bus = Bus(store)
    sv = Services(lab_vivo=LabVivoLectura(bus, dir=tmp_path))
    async with TestClient(TestServer(make_app(bus, lambda: {}, sv))) as client:
        j = await (await client.get("/api/lab_vivo")).json()
        assert j["estado"] == "activo" and len(j["mejores"]) == 20 and j["diario"]["texto"] == ["hoy"]
        j = await (await client.get("/api/lab_vivo/hipotesis?familia=keltner&limite=1")).json()
        assert [h["id"] for h in j["hipotesis"]] == [HID] and j["n"] == 2
        assert (await (await client.get(f"/api/lab_vivo/hipotesis?id={HID}")).json())["variantes"][0]["id"] == f"{HID}:V1"
        for url, code in ((f"/api/lab_vivo/hipotesis?id=..%2Festado", 400), ("/api/lab_vivo/hipotesis?id=h-20261029-1130-nada", 404),
                          ("/api/lab_vivo/hipotesis?estado=../x", 400), ("/api/lab_vivo/hipotesis?limite=x", 400),
                          ("/api/lab_vivo/diario?dia=29-10-2026", 400), ("/api/lab_vivo/diario?dia=2026-01-01", 404),
                          ("/api/lab_vivo/generador?limite=no", 400)):
            assert (await client.get(url)).status == code, url
        assert (await (await client.get("/api/lab_vivo/diario")).json())["texto"] == ["hoy"]
        assert (await (await client.get("/api/lab_vivo/cola")).json())["pendientes"] == 4
        assert len((await (await client.get("/api/lab_vivo/generador?limite=2")).json())["notas"]) == 2
    store.close()


async def test_rutas_sin_el_modulo_leen_la_carpeta_por_defecto(tmp_path, monkeypatch):
    """Sin Services.lab_vivo, el servidor crea su propia lectura (solo archivos) en DATA_DIR/lab_vivo."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.delenv("BOT_LAB_VIVO_DIR", raising=False)
    store = Store(tmp_path / "t.db")
    bus = Bus(store)
    async with TestClient(TestServer(make_app(bus, lambda: {}))) as client:
        assert (await (await client.get("/api/lab_vivo")).json())["estado"] == "apagado"
        assert (await (await client.get("/api/lab_vivo/cola")).json())["estado"] == "apagado"
        assert (await client.get("/api/lab_vivo/diario")).status == 404
        preparar(tmp_path / "lab_vivo")
        assert (await (await client.get("/api/lab_vivo")).json())["estado"] == "activo"
    store.close()


async def _orden_web(q, n=1, tries=100):
    out = []
    for _ in range(tries):
        while not q.empty():
            m = json.loads(q.get_nowait())
            if m.get("type") == "orden_web" and m.get("area") == "lab_vivo":
                out.append(m)
        if len(out) >= n:
            break
        await asyncio.sleep(0.02)
    return out


async def test_mando_de_la_web_solo_con_el_origen_estricto(tmp_path):
    """{"type": "lab_vivo", "accion": "pausar|reanudar"}: sin Origin (otro proceso del PC) no pasa; con el de la
    propia web, sí; el resultado sale en «orden_web» y el estado «lab_vivo» se vuelve a publicar al momento."""
    preparar(tmp_path)
    store = Store(tmp_path / "t.db")
    bus = Bus(store)
    q = bus.subscribe()
    sv = Services(lab_vivo=LabVivoLectura(bus, dir=tmp_path))
    async with TestClient(TestServer(make_app(bus, lambda: {}, sv))) as client:
        ws = await client.ws_connect("/events")
        await ws.receive()
        await ws.send_str(json.dumps({"type": "lab_vivo", "accion": "pausar"}))
        got = await _orden_web(q)
        await ws.close()
        assert got[0]["ok"] is False and "propia web" in got[0]["why"] and not (tmp_path / "pausa_manual").exists()
        ws = await client.ws_connect("/events", origin=f"http://127.0.0.1:{config.PORT}")
        await ws.receive()
        for m in ({"type": "lab_vivo", "accion": "pausar"}, {"type": "lab_vivo", "accion": "borrar_todo"},
                  {"type": "lab_vivo", "accion": "reanudar"}):
            await ws.send_str(json.dumps(m))
            if m["accion"] == "pausar":
                got = await _orden_web(q)
                assert got[0]["ok"] is True and got[0]["accion"] == "pausar" and (tmp_path / "pausa_manual").exists()
                assert bus.state["lab_vivo"]["pausa_manual"] is True
        got = await _orden_web(q, 2)
        await ws.close()
        assert [m["ok"] for m in got] == [False, True] and not (tmp_path / "pausa_manual").exists()
        assert bus.state["lab_vivo"]["pausa_manual"] is False
    store.close()
