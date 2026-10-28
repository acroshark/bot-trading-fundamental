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
