"""Informe de calidad del histórico de noticias (bot/noticias_informe.py): el acierto ajustado es el de controles/fuentes
(rendimiento a 4 h − deriva del día), la unidad es historia×moneda, el consenso de los bruscos es el signo de la suma de
sentidos de las 12 h antes y los precios no miran al futuro. Sin red ni datos reales."""
import json
from datetime import datetime, timezone

import numpy as np
import pytest

from bot import config
from bot import noticias_informe as ni
from bot.lab_vivo import datos

MIN, H, DAY = 60_000, 3_600_000, 86_400_000
T0 = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp() * 1000)


def test_unidades_por_moneda_y_las_generales_para_las_cuatro():
    hs = [{"id": "a", "uso": T0, "t0": T0, "dom": "x.com", "fuente": "wp", "clase": "cripto"},
          {"id": "b", "uso": T0, "t0": T0, "dom": "y.com", "fuente": "gdelt", "clase": "macro"}]
    tri = {"a": {"triage": {"repercusion": {"direccion": 40}, "monedas": ["ETH"], "tipo": "exchange", "importancia": 6}},
           "b": {"triage": {"repercusion": {"direccion": -10}, "monedas": [], "tipo": "macro", "importancia": 3}}}
    us = ni._unidades(hs, tri)
    assert [(u["id"], u["coin"], u["sen"]) for u in us] == [("a", "ETH", 1)] + [("b", c, 0) for c in config.COINS]
    assert us[1]["general"] and not us[0]["general"]


def test_acierto_ajustado_resta_la_deriva_del_dia():
    us = [{"id": "a", "uso": T0, "sen": 1, "r240": 0.010, "der": 0.004},     # sube más que el día: acierta
          {"id": "b", "uso": T0 + H, "sen": 1, "r240": 0.002, "der": 0.004},  # sube menos: falla ajustado, acierta bruto
          {"id": "c", "uso": T0 + 2 * H, "sen": -1, "r240": -0.01, "der": 0.0},
          {"id": "d", "uso": T0 + 3 * H, "sen": 0, "r240": 0.05, "der": 0.0},   # sin sentido: no cuenta
          {"id": "e", "uso": T0 + 4 * H, "sen": 1, "r240": None, "der": 0.0}]   # sin medida: no cuenta
    a = ni._acierto(us)
    assert a["n"] == 3 and a["acierto_4h_ajustado"] == round(2 / 3, 4) and a["acierto_4h_bruto"] == 1.0
    assert a["horas"] == 3 and 0 < a["wilson90_inf_horas"] < 2 / 3
    assert ni._acierto([]) == {"n": 0}


def test_precios_sin_futuro_y_deriva_del_dia(monkeypatch):
    n = 3 * 1440
    t = T0 + np.arange(n, dtype=np.int64) * MIN
    c = 100 * (1 + 0.0001 * np.arange(n))
    v = datos.Velas(t, c, c, c, c, np.ones(n), np.ones(n))
    monkeypatch.setattr(datos, "archivos_moneda", lambda coin, meses: [])
    monkeypatch.setattr(datos, "cargar_velas", lambda paths, a, b: v)
    pr = ni.Precios(T0, T0 + 2 * DAY)
    # en t solo vale la vela que cierra en t (la que abre en t − 1 min): la de t aún no ha cerrado
    assert pr.cierre("BTC", [T0 + 10 * MIN])[0] == c[9]
    assert np.isnan(pr.cierre("BTC", [T0])[0])
    r = pr.deriva_en("BTC", np.array([T0 // DAY]))[0]
    assert r == pytest.approx(np.mean([c[i + 240] / c[i] - 1 for i in range(1440)]), rel=1e-12)


def test_consenso_de_los_bruscos(tmp_path, monkeypatch):
    bt = tmp_path / "backtest" / "controles" / "bruscos"
    bt.mkdir(parents=True)
    eps = [{"id": "E1", "moneda": "BTC", "sentido": 1, "ventana_antes": {"desde_ms": T0, "hasta_ms": T0 + 12 * H}},
           {"id": "E2", "moneda": "BTC", "sentido": -1, "ventana_antes": {"desde_ms": T0 + DAY, "hasta_ms": T0 + DAY + 12 * H}},
           {"id": "E3", "moneda": "ETH", "sentido": 1, "ventana_antes": {"desde_ms": T0, "hasta_ms": T0 + 12 * H}}]
    ventanas = [{"id": "E1-C1", "tipo": "control", "episodio": "E1", "moneda": "BTC", "desde_ms": T0 + 7 * DAY,
                 "hasta_ms": T0 + 7 * DAY + 12 * H}]
    (bt / "episodios.json").write_text(json.dumps({"episodios": eps, "ventanas": ventanas}), "utf-8")
    (bt / "resultado.json").write_text(json.dumps({"episodios": {"lista_160": [
        {"episodio": "E1", "medido": True}, {"episodio": "E2", "medido": True}, {"episodio": "E3", "medido": False}]}}),
        "utf-8")
    monkeypatch.setattr(config, "BACKTEST_DIR", tmp_path / "backtest")
    us = [{"coin": "BTC", "uso": T0 + H, "sen": 1}, {"coin": "BTC", "uso": T0 + 2 * H, "sen": 1},
          {"coin": "BTC", "uso": T0 + DAY + H, "sen": 1},                       # E2 cae: el consenso falla
          {"coin": "BTC", "uso": T0 + 7 * DAY + H, "sen": 1},                   # control en el sentido de E1: alarma
          {"coin": "ETH", "uso": T0 + 13 * H, "sen": 1}]                         # fuera de la ventana de E3
    r = ni._bruscos(us)
    assert r["los_54"] == {"episodios": 2, "con_consenso": 2, "aciertos": 1, "acierto": 0.5, "sin_noticias_o_empate": 0}
    assert r["los_160"]["sin_noticias_o_empate"] == 1 and r["controles"]["consenso_en_el_sentido_del_episodio"] == 1


def test_markdown_con_el_precio_pendiente():
    r = {"generado": "2026-10-29T23:00:00+00:00",
         "descarga": {"gdelt": {"dias_hechos": 5, "dias_periodo": 10, "dias_sin_archivos_en_gdelt": ["2025-06-01"],
                                "dias_por_hacer": 5},
                      "wp": {"meses_periodo": 2, "por_sitio": {"a.com": {"meses": 2}}}},
         "seleccion": {"regla": {"racimo_n": {"cripto": 2, "macro": 4}}, "seleccionadas": 3, "triadas": 0,
                       "por_clase": {"cripto": 3}},
         "precio": "pendiente: aún no hay historias triadas"}
    md = ni.a_markdown(r)
    assert "5 de 10 días" in md and "pendiente" in md and md.startswith("# Histórico de noticias")
