"""Resúmenes del backtest para la pestaña Backtest (bot/resumen_backtest.py) y su ruta /api/backtest/impulso."""
import json

from aiohttp.test_utils import TestClient, TestServer

from bot import config, resumen_backtest as rb
from bot.bus import Bus
from bot.server import make_app
from bot.store import Store

H = 3600_000


def _impulso(n_out: int = 400) -> dict:
    curva = [[1_700_000_000_000 + i * H, float(v)] for i, v in enumerate([1, 3, 2, -1, 4])]
    fuera = [[1_800_000_000_000 + i * H, round(i * 0.1, 3)] for i in range(n_out)]
    fuera[200][1] = -5.0   # caída honda en mitad de la curva: no puede perderse al reducir puntos
    return {"generated": 1, "split": 1_750_000_000_000, "sl_pct": 3.0, "nota": "elegida después",
            "fuera": {"n": n_out, "mean_R": 0.35, "salidas": {"stop": 1}, "curva_R": fuera,
                      "largos": {"n": 1, "mean_R": 0.1}, "cortos": {"n": 2, "mean_R": 0.5}, "desde": 1, "hasta": 2},
            "dentro": {"n": 5, "mean_R": 0.13, "curva_R": curva}}


def test_impulso_reduce_la_curva_sin_perder_la_caida_ni_el_total():
    s = rb.impulso_summary(_impulso(), points=50)
    f = s["fuera"]
    assert len(f["curva_R"]) == 51 and f["curva_R"][-1] == [1_800_000_000_000 + 399 * H, 39.9]
    assert f["total_R"] == 39.9 and f["max_dd_R"] == round(19.9 + 5.0, 2)   # pico 19,9 R antes de caer a −5 R
    assert s["dentro"]["max_dd_R"] == 4.0 and s["dentro"]["total_R"] == 4.0 and len(s["dentro"]["curva_R"]) == 5
    assert s["nota"] == "elegida después" and f["cortos"] == {"n": 2, "mean_R": 0.5}


def test_laboratorio_con_cuenta_en_avance():
    lab = {"summary": {"tested": 2000, "promising": 2, "robust": 0, "expected_by_chance": 9},
           "promising": [{"id": "ap_mom:15:x", "name": "E2", "is": {"trades": 557, "mean_R": 0.04, "t": 1},
                          "oos": {"trades": 278, "mean_R": 0.14, "win_rate": 0.52}}],
           "walk_forward": {"account_quarter": {"from": "2024-04-01", "top": 10, "period": "quarter",
                                                "riesgo_0_5_lev1": {"days": 668, "trades": 1356, "total_pct": -61.17,
                                                                    "max_dd_pct": 65.28, "mean_pct": -0.13}}}}
    s = rb.lab_summary(lab)
    assert s["avance"] == {"days": 668, "trades": 1356, "total_pct": -61.17, "max_dd_pct": 65.28,
                           "desde": "2024-04-01", "top": 10, "periodo": "quarter"}
    assert s["promising"][0]["is"] == {"trades": 557, "mean_R": 0.04} and rb.lab_summary({})["avance"] is None


async def test_ruta_impulso(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKTEST_DIR", tmp_path)
    store = Store(tmp_path / "bot.db")
    async with TestClient(TestServer(make_app(Bus(store), lambda: {}))) as client:
        r = await client.get("/api/backtest/impulso")
        assert r.status == 404
        (tmp_path / "impulso_7d.json").write_text(json.dumps(_impulso()), encoding="utf-8")
        (tmp_path / "laboratorio.json").write_text("{roto", encoding="utf-8")   # ilegible: va como None, no rompe
        r = await client.get("/api/backtest/impulso")
        assert r.status == 200
        d = await r.json()
        assert d["lab"] is None and d["impulso"]["fuera"]["total_R"] == 39.9
        assert len(d["impulso"]["fuera"]["curva_R"]) == rb.CURVE_POINTS + 1
    store.close()


def _seis() -> dict:
    d = _impulso(300)
    d.update(sl_pct=1.5, nota="réplica del motor", nombre="6 diarias",
             params={"measure_min": 15, "min_move_pct": 0.0, "entry": "t:15", "sl_pct": 1.5, "target_r": 3.0,
                     "max_hold_h": 24, "news_mode": "fiables", "max_day_entries": 6})
    d["fuera"]["mean_R"] = -0.12
    return d


def test_seis_diarias_con_nombre_y_parametros():
    s = rb.seis_summary(_seis(), points=50)
    assert s["nombre"] == "6 diarias" and s["params"]["max_day_entries"] == 6 and s["sl_pct"] == 1.5
    assert len(s["fuera"]["curva_R"]) == 51 and s["fuera"]["mean_R"] == -0.12 and s["fuera"]["max_dd_R"] is not None
    assert rb.seis_summary({"fuera": {}, "params": "roto"})["params"] is None


async def test_ruta_con_seis(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKTEST_DIR", tmp_path)
    store = Store(tmp_path / "bot.db")
    (tmp_path / rb.SEIS_FILE).write_text(json.dumps(_seis()), encoding="utf-8")
    assert set(rb.load()) == {"impulso", "lab", "seis", "seis2", "ruptura3", "costes"}   # costes: con qué comisiones
    async with TestClient(TestServer(make_app(Bus(store), lambda: {}))) as client:
        r = await client.get("/api/backtest/impulso")   # solo con «6 diarias» ya responde
        assert r.status == 200
        d = await r.json()
        assert d["impulso"] is None and d["lab"] is None and d["seis"]["nombre"] == "6 diarias"
        assert d["seis"]["fuera"]["total_R"] == 29.9
    store.close()


def test_incrustar_y_quitar_el_bloque_de_seis(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    from bot import server
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("incrustar_backtest", root / "scripts" / "incrustar_backtest.py")
    inc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inc)
    page = tmp_path / "radar.html"
    page.write_text('<html><!--BT--><script type="application/json" id="seisData">{"viejo":1}</script></html>',
                    encoding="utf-8")
    bt = tmp_path / "bt"
    bt.mkdir()
    (bt / "resultados.json").write_text(json.dumps({"strategies": []}), encoding="utf-8")
    (bt / rb.SEIS_FILE).write_text(json.dumps(_seis()), encoding="utf-8")
    monkeypatch.setattr(inc, "PAGE", page)
    monkeypatch.setattr(config, "BACKTEST_DIR", bt)
    inc.main()
    html = page.read_text(encoding="utf-8")
    m = inc.BLOCK_SEIS.search(html)
    assert m and '"nombre":"6 diarias"' in m.group(0) and '"viejo"' not in html and 'id="btData"' in html
    assert "seisData" not in server.BT_BLOCK.sub("<!--BT-->", html)   # en vivo llega por /api/backtest/impulso


def _seis2() -> dict:
    d = _impulso(300)
    d.update(sl_pct=2.0, nota="réplica del motor con SEIS2", nombre="6 diarias v2",
             params={"measure_min": 15, "entry": "t:15", "sl_pct": 2.0, "target_r": None, "tp1_r": 3.0,
                     "tp1_frac": 0.5, "lock_r": 3.0, "floor_r": 2.0, "trail_r": 2.0, "max_hold_h": 24,
                     "max_hold_active_h": 168})
    d["fuera"].update(mean_R=-0.075, salidas={"stop": 675, "tiempo": 670, "seguimiento": 62, "cerrojo": 57})
    return d


async def test_seis2_en_la_ruta_y_en_la_demostracion(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    from bot import server
    monkeypatch.setattr(config, "BACKTEST_DIR", tmp_path)
    store = Store(tmp_path / "bot.db")
    (tmp_path / rb.SEIS2_FILE).write_text(json.dumps(_seis2()), encoding="utf-8")
    res = rb.load()
    assert set(res) == {"impulso", "lab", "seis", "seis2", "ruptura3", "costes"} and res["seis"] is None
    s2 = res["seis2"]
    assert s2["nombre"] == "6 diarias v2" and s2["params"]["lock_r"] == 3.0 and s2["params"]["target_r"] is None
    assert s2["fuera"]["salidas"]["cerrojo"] == 57 and len(s2["fuera"]["curva_R"]) == rb.CURVE_POINTS + 1
    async with TestClient(TestServer(make_app(Bus(store), lambda: {}))) as client:
        r = await client.get("/api/backtest/impulso")   # solo con «6 diarias v2» ya responde
        assert r.status == 200
        d = await r.json()
        assert d["seis2"]["nombre"] == "6 diarias v2" and d["seis2"]["fuera"]["mean_R"] == -0.075
    store.close()
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("incrustar_backtest", root / "scripts" / "incrustar_backtest.py")
    inc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inc)
    page = tmp_path / "radar.html"
    page.write_text('<html><!--BT--><script type="application/json" id="seisData">{"viejo":1}</script></html>',
                    encoding="utf-8")
    bt = tmp_path / "bt"
    bt.mkdir()
    (bt / "resultados.json").write_text(json.dumps({"strategies": []}), encoding="utf-8")
    (bt / rb.SEIS_FILE).write_text(json.dumps(_seis()), encoding="utf-8")
    (bt / rb.SEIS2_FILE).write_text(json.dumps(_seis2()), encoding="utf-8")
    monkeypatch.setattr(inc, "PAGE", page)
    monkeypatch.setattr(config, "BACKTEST_DIR", bt)
    inc.main()
    html = page.read_text(encoding="utf-8")
    m2, m1 = inc.BLOCK_SEIS2.search(html), inc.BLOCK_SEIS.search(html)
    assert m2 and '"nombre":"6 diarias v2"' in m2.group(0) and m1.end() < m2.start()   # justo detrás de seisData
    inc.main()                                                                         # otra vez: se reemplaza
    assert page.read_text(encoding="utf-8").count('id="seis2Data"') == 1
    live = server.BT_BLOCK.sub("<!--BT-->", page.read_text(encoding="utf-8"))
    assert "seis2Data" not in live and "seisData" not in live   # en vivo llega por /api/backtest/impulso


def test_cuentas_por_operacion_con_la_curva_completa():
    """Con un punto de la curva por operación: la mejor y la peor, y cuántas ganadoras ganan menos de 2R y de 1R (el
    cierre por tiempo sale con lo que haya). Se calculan antes de reducir la curva; sin un punto por operación, nada."""
    rs = [-1.1, 0.4, 2.9, -1.1, 1.5, 5.7, 0.05, -0.3] * 30           # 240 operaciones
    acc, curva = 0.0, []
    for i, r in enumerate(rs):
        acc = round(acc + r, 3)
        curva.append([1_800_000_000_000 + i * H, acc])
    d = {"fuera": {"n": len(rs), "mean_R": 0.0, "curva_R": curva}, "dentro": {"n": 7, "curva_R": curva[:5]}}
    s = rb.seis_summary(d, points=50)
    f = s["fuera"]
    assert (f["max_R"], f["min_R"]) == (5.7, -1.1) and f["ganadoras_n"] == 150
    assert f["ganadoras_bajo_2R"] == 90 and f["ganadoras_bajo_1R"] == 60 and len(f["curva_R"]) == 51
    assert "max_R" not in s["dentro"] and "ganadoras_n" not in s["dentro"]   # 5 puntos para 7 operaciones: no se sabe
    assert rb.per_trade([], 0) == {} and rb.per_trade(curva, None) == {}


def test_cada_resumen_dice_con_que_comisiones_esta_calculado():
    """Las réplicas del motor van con config.FEE_PCT (0,10 % por lado, también en perpetuos) y 2 pb: el perfil «OKX EU
    con cuenta X-Perps» (tomador del contado); el laboratorio, con las de su archivo. Y el aviso de sin X-Perps."""
    s = rb.impulso_summary(_impulso(), points=50)
    c = s["costes"]
    assert c["perfil"] == "okx_eu_con_xperps" and c["comision_pct"] == 0.10 and c["deslizamiento_bps"] == 2.0
    assert c["coste_rt_pct"] == 0.24 and "0,10 % de comisión por lado (también en los perpetuos)" in c["texto"]
    assert "0,35 %" in c["aviso"] and "no hay cortos" in c["aviso"] and c["perfil_en_uso"] == "okx_eu_con_xperps"
    lab = rb.lab_summary({"params": {"fee_pct": 0.1, "slip_bps": 2.0, "fee_perp_pct": 0.05}})
    assert lab["costes"]["comision_perp_pct"] == 0.05 and "perpetuos 0,05 %" in lab["costes"]["texto"]
    caro = rb.costes(0.35)
    assert caro["perfil"] is None and caro["coste_rt_pct"] == 0.74 and "X-Perps»" not in caro["texto"]


async def test_ruta_impulso_lleva_los_costes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKTEST_DIR", tmp_path)
    (tmp_path / "impulso_7d.json").write_text(json.dumps(_impulso()), encoding="utf-8")
    store = Store(tmp_path / "bot.db")
    async with TestClient(TestServer(make_app(Bus(store), lambda: {}))) as client:
        d = await (await client.get("/api/backtest/impulso")).json()
        assert d["costes"]["perfil"] == "okx_eu_con_xperps" and d["impulso"]["costes"]["comision_pct"] == 0.1
    store.close()


def _rup3() -> dict:
    d = _impulso(300)
    d.update(sl_pct=2.0, nota="réplica del motor con RUPTURA3", nombre="Ruptura V3: rango de apertura con tendencia, volumen y régimen",
             params={"modelo": "ruptura3", "measure_min": 15, "entry": "brk", "entry_end_min": 240, "sl_pct": 2.0,
                     "rp_stop_max_pct": 3.0, "rp_max_apertura": 2, "tp1_r": 3.0, "tp1_frac": 0.5, "lock_r": 3.0})
    d["fuera"].update(mean_R=0.4288, op_jornada=0.386,
                      antes_de_costes={"n": 118, "R_bruto": 0.5392, "costes_R": -0.1107, "R_neto": 0.4285})
    d["dentro"]["antes_de_costes"] = {"n": 159, "R_bruto": 0.0868, "costes_R": -0.1198, "R_neto": -0.033}
    return d


async def test_ruptura3_en_la_ruta_y_en_la_demostracion(tmp_path, monkeypatch):
    """ruptura3_resumen.json (scripts/replica_ruptura3.py) sale en /api/backtest/impulso con «antes_de_costes» dentro de
    fuera y dentro, y se incrusta en la demostración (ruptura3Data) detrás de seis2Data; en vivo se quita."""
    import importlib.util
    from pathlib import Path
    from bot import server
    monkeypatch.setattr(config, "BACKTEST_DIR", tmp_path)
    store = Store(tmp_path / "bot.db")
    (tmp_path / rb.RUPTURA3_FILE).write_text(json.dumps(_rup3()), encoding="utf-8")
    res = rb.load()
    assert res["seis2"] is None and res["ruptura3"]["nombre"].startswith("Ruptura V3")
    f = res["ruptura3"]["fuera"]
    assert f["antes_de_costes"]["R_bruto"] == 0.5392 and f["op_jornada"] == 0.386 and len(f["curva_R"]) == rb.CURVE_POINTS + 1
    assert res["ruptura3"]["dentro"]["antes_de_costes"]["R_neto"] == -0.033 and res["ruptura3"]["costes"]["comision_pct"] == 0.1
    async with TestClient(TestServer(make_app(Bus(store), lambda: {}))) as client:
        r = await client.get("/api/backtest/impulso")   # solo con la Ruptura V3 ya responde
        assert r.status == 200
        d = await r.json()
        assert d["ruptura3"]["params"]["modelo"] == "ruptura3" and d["ruptura3"]["fuera"]["mean_R"] == 0.4288
    store.close()
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("incrustar_backtest", root / "scripts" / "incrustar_backtest.py")
    inc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inc)
    page = tmp_path / "radar.html"
    page.write_text('<html><!--BT--></html>', encoding="utf-8")
    bt = tmp_path / "bt"
    bt.mkdir()
    (bt / "resultados.json").write_text(json.dumps({"strategies": []}), encoding="utf-8")
    (bt / rb.SEIS2_FILE).write_text(json.dumps(_seis2()), encoding="utf-8")
    (bt / rb.RUPTURA3_FILE).write_text(json.dumps(_rup3()), encoding="utf-8")
    monkeypatch.setattr(inc, "PAGE", page)
    monkeypatch.setattr(config, "BACKTEST_DIR", bt)
    inc.main()
    html = page.read_text(encoding="utf-8")
    m3, m2 = inc.BLOCK_RUP3.search(html), inc.BLOCK_SEIS2.search(html)
    assert m3 and '"modelo":"ruptura3"' in m3.group(0) and m2.end() < m3.start()   # justo detrás de seis2Data
    inc.main()                                                                       # otra vez: se reemplaza
    assert page.read_text(encoding="utf-8").count('id="ruptura3Data"') == 1
    assert "ruptura3Data" not in server.BT_BLOCK.sub("<!--BT-->", page.read_text(encoding="utf-8"))
