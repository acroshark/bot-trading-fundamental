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
    assert set(rb.load()) == {"impulso", "lab", "seis"}
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
