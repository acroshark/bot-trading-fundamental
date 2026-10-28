"""Skill radar de OpenClaw (openclaw-trading/workspace/skills/radar/scripts/radar.py): resume la API local del bot para
la IA local. Revisión del 28-10-2026 (hallazgo n.º 7): ya no enseña índices; «tendencia» cuenta cómo se mueve la cripto
en cada apertura (/api/cripto_aperturas) y «predicciones» solo trae las de cripto. Sin red: get() falso."""
import importlib.util
import urllib.error
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "openclaw-trading" / "workspace" / "skills" / "radar" / "scripts" / "radar.py"
INDICES = ("Nikkei", "KOSPI", "DAX", "FTSE", "IBEX", "S&P", "Hang Seng", "Ibovespa", "SSE", "DFM")


@pytest.fixture
def radar():
    spec = importlib.util.spec_from_file_location("radar_skill", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CA = {"type": "cripto_aperturas", "ts": 1, "coins": ["BTC", "ETH", "SOL", "XRP"], "markets": {
    "tokio": {"name": "Tokio", "window": "tokio-seul", "state": "en_curso", "open_ms": 1_793_000_000_000,
              "minutes_since_open": 70, "coins": {"BTC": {"ret_pct": 0.42, "range_pct": 1.1}, "ETH": {"ret_pct": -0.2, "range_pct": 1.8}},
              "last": None, "hist": {"n": 377, "desde": "2023-11-01", "coins": {"BTC": {"up_rate": 0.5, "range_ge2_rate": 0.2}}}},
    "nuevayork": {"name": "Nueva York", "state": "por_abrir", "open_ms": None, "coins": None,
                  "last": {"open_ms": 1_792_900_000_000, "coins": {"BTC": {"ret_4h_pct": 0.6, "range_4h_pct": 1.9}}},
                  "hist": None, "aviso": "Faltan velas de BTC"}},
      "total": {"n": 3771, "desde": "2023-11-01", "any_range_ge2_rate": 0.764, "any_range_ge2_8h_rate": 0.933,
                "coins": {"BTC": {"range_ge2_rate": 0.299}, "SOL": {"range_ge2_rate": 0.695}}},
      "chain": {"after_up_rate": 0.49, "after_down_rate": 0.511, "base_rate": 0.501, "n": 15076,
                "nota": "Tras una apertura… no predice."}}
TREND = {"trend": {"markets": {"tokio": {"name": "Tokio", "index": "Nikkei 225", "change_pct": 1.2}}},
         "orderflow": {"coins": {"BTC": {"trend": "alcista", "imbalance": 0.2, "buy_ratio_5m": 0.6, "cvd_15m": 3.0,
                                         "vol_5m_usd": 4e6, "vol_ratio": 1.4, "ret_15m_pct": 0.1, "ret_60m_pct": 0.3}}}}
PRED = {"items": [{"kind": "bolsa", "label": "Tokio (Nikkei 225)", "event": "Apertura", "horizon": "sesión", "prob_up": 0.6,
                   "confidence": "media", "when_utc": 1, "drivers": []},
                  {"kind": "cripto", "label": "BTC · Apertura Londres", "event": "Apertura Londres", "horizon": "4 h",
                   "prob_up": 0.52, "expected_pct": 0.1, "confidence": "baja", "n_samples": 900, "when_utc": 2,
                   "drivers": [{"factor": "Apertura anterior", "effect_pp": 0.0}]}],
        "accuracy": {"evaluated": 50, "hit_rate": 0.7, "by_kind": {"cripto": {"evaluated": 20, "hit_rate": 0.5}}}}


def test_tendencia_cuenta_la_cripto_en_cada_apertura_y_no_el_indice(radar, monkeypatch):
    monkeypatch.setattr(radar, "get", lambda path: {"/api/cripto_aperturas": CA, "/api/tendencia": TREND}[path])
    out = radar.main(["tendencia"])
    assert "Tokio: en curso" in out and "desde la apertura (70 min)" in out and "BTC +0,42 % (rango 1,10 %)" in out
    assert "Nueva York: por abrir" in out and "su última apertura, a las 4 h: BTC +0,60 %" in out and "aviso: Faltan velas" in out
    assert "histórico (377 aperturas desde 2023-11-01)" in out and "no una previsión" in out
    assert "Todas las aperturas juntas (3771" in out and "alguna de las 4, 76 %" in out and "en 8 h, 93 %" in out
    assert "la siguiente sube el 49 %" in out and "NO predice" in out and "Libro de órdenes" in out
    assert not any(x in out for x in INDICES) and "1,20" not in out


def test_tendencia_con_el_bot_antiguo_o_arrancando(radar, monkeypatch):
    def get(path):
        if path == "/api/cripto_aperturas":
            raise urllib.error.HTTPError(path, 404, "no", {}, None)
        return TREND
    monkeypatch.setattr(radar, "get", get)
    assert "hay que reiniciarlo" in radar.main(["tendencia"])
    monkeypatch.setattr(radar, "get", lambda p: {"markets": {}, "estado": "arrancando"} if "cripto" in p else TREND)
    assert "calculando lo histórico" in radar.main(["tendencia"])


def test_predicciones_solo_de_cripto(radar, monkeypatch):
    monkeypatch.setattr(radar, "get", lambda path: PRED)
    out = radar.main(["predicciones"])
    assert "BTC · Apertura Londres" in out and "52 % de que suba" in out
    assert "Nikkei" not in out and "Tokio" not in out
    assert "Acierto medido de las de cripto: 50 % en 20 evaluadas" in out and "70 %" not in out
    monkeypatch.setattr(radar, "get", lambda path: {"items": PRED["items"][:1]})
    assert radar.main(["predicciones"]) == "Aún no hay predicciones de cripto."
