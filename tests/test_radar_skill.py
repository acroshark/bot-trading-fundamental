"""Skill radar de OpenClaw (openclaw-trading/workspace/skills/radar/scripts/radar.py): resume la API local del bot para
la IA local. Revisión del 28-10-2026 (hallazgo n.º 7): ya no enseña índices; «tendencia» cuenta cómo se mueve la cripto
en cada apertura (/api/cripto_aperturas) y «predicciones» solo trae las de cripto. Hallazgos n.º 17 y 18: comandos de
estrategia, aprendizaje, post-mortems, evolución, propuestas, fuentes vivas, vigilancia y noticias fiables; la balanza
cuenta lo que cuenta el motor; backtest lee /api/backtest/impulso; y la lista de permitidos de instalar.py. Sin red:
get() falso."""
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


# ---- revisión del 28-10-2026 (hallazgos n.º 17 y 18): comandos nuevos, balanza, backtest del motor y permisos

INSTALAR = Path(__file__).resolve().parents[1] / "openclaw-trading" / "instalar.py"
SKILL_MD = SCRIPT.parents[1] / "SKILL.md"
AGENTS_MD = SCRIPT.parents[3] / "AGENTS.md"
RADAR_WIN = r"M:\BotFundamental-openclaw\workspace\skills\radar\scripts\radar.py"


def falso(rutas: dict, llamadas: list | None = None):
    """get() falso: devuelve la respuesta de la ruta o un 404, como el bot sin ese módulo o sin datos."""
    def get(path):
        if llamadas is not None:
            llamadas.append(path)
        if path not in rutas:
            raise urllib.error.HTTPError(path, 404, "no", {}, None)
        return rutas[path]
    return get


def noticia(titular, d, cred_aj, cred=80, estado="activa", activos=("BTC",), clase="cripto", imp=8, reflejo=None):
    return {"hora": "28-10 12:00", "titular": titular, "fuente": "F", "importancia": imp, "credibilidad": cred,
            "credibilidad_ajustada": cred_aj, "estado_fuente": estado, "clase": clase, "reflejo": reflejo,
            "repercusion": {"direccion": d, "magnitud": 50, "activos": list(activos)}}


def test_balanza_un_cero_cuenta_como_cero_y_las_silenciadas_no_cuentan(radar, monkeypatch):
    news = [noticia("Macro sin reflejo", 60, 0, cred=90, clase="macro"),                 # antes contaba con 90
            noticia("Fuente silenciada", -60, 80, estado="silenciada"),
            noticia("Fuente retirada", -60, 80, estado="retirada"),
            noticia("Macro sin credibilidad ni reflejo", 60, None, cred=None, clase="macro"),
            noticia("ETF de ETH aprobado", 50, 70, activos=("ETH",), imp=8),
            noticia("SOL sin ajuste", -40, None, cred=50, activos=("SOL",), imp=5)]
    monkeypatch.setattr(radar, "get", falso({"/api/noticias?horas=6&min=4": news}))
    out = radar.main(["balanza"])
    assert "- BTC: sin noticias con dirección" in out and "- XRP: sin noticias con dirección" in out
    assert "- ETH: 100 % compra / 0 % venta (peso 0,14)" in out          # 0,5 × 0,5 × 0,7 × 0,8
    assert "- SOL: 0 % compra / 100 % venta (peso 0,05)" in out          # sin ajustada, la del triaje: 0,4 × 0,5 × 0,5 × 0,5
    assert "Macro sin reflejo" not in out and "silenciada (" not in out and "Fuente retirada" not in out
    assert "No cuentan, como en el motor: 2 de fuentes silenciadas o retiradas y 2 con credibilidad ajustada 0" in out
    assert "consejo de inversión" in out


def test_balanza_con_una_macro_confirmada_y_peso_completo(radar, monkeypatch):
    news = [noticia("Fed recorta tipos", 60, 85, clase="macro", reflejo={"estado": "confirmada"}, activos=())]
    monkeypatch.setattr(radar, "get", falso({"/api/noticias?horas=6&min=4": news}))
    out = radar.main(["balanza"])
    assert all(f"- {c}: 100 % compra" in out for c in ("BTC", "ETH", "SOL", "XRP")) and "No cuentan" not in out


OPEN_SEIS = {"type": "openings", "strategy": "impulso", "paused": False, "notice": None, "params_source": "usuario",
             "params": {"preset": "seis", "nombre": "6 diarias", "descripcion": "Impulso de los primeros 15 min sin filtros",
                        "measure_min": 15, "min_move_pct": 0.0, "entry": "t:15", "rvol_min": None, "btc_filter": "ninguno",
                        "news_mode": "fiables", "sl_pct": 1.5, "target_r": 3.0, "max_hold_h": 24.0, "min_net_pct": 3.0,
                        "long_instrument": "spot", "short_instrument": "perp", "windows": ["tokio-seul", "europa"],
                        "coins": ["BTC", "ETH"]},
             "estrategia_cargada": {"firma": "3f2a1b2c9d8e", "ts": 1_793_195_340_000, "pausada": False, "fuente": "usuario",
                                    "registro": "v12", "preset": "seis"},
             "day": {"date": "2026-10-28", "entries": 2, "risk_pct": 1.0, "max_entries": 6, "max_risk_pct": 3.0},
             "windows": [{"id": "europa-2026-10-28", "name": "Europa", "open_utc": 1_793_174_400_000, "state": "en_posicion",
                          "why": "Se sigue el impulso de los 15 primeros min en su sentido",
                          "coins": {"BTC": {"state": "en_posicion", "signal": 1, "instrument": "spot", "rvol": 3.0,
                                            "entries": 1, "why": "Impulso de +0,4 % en 15 min",
                                            "plan": {"entry": 100.0, "stop": 98.5, "target": 104.5, "stopPct": 1.5,
                                                     "rr": 3.0, "net_pct": 4.26, "cost_pct": 0.24, "ok": True}},
                                    "ETH": {"state": "terminada", "signal": -1, "instrument": "perp", "rvol": None,
                                            "entries": 0, "why": "Sin entrada"}}}]}
LAB = {"period": {"from": "2023-11-01", "to": "2026-10-23", "split": "2025-08-16"},
       "summary": {"tested": 2000, "promising": 2, "robust": 0, "expected_by_chance": 9},
       "promising": [{"id": "ap_mom:15:0,2|rvol:1,5|todos|a_favor|todas|todas|todos|t:30|3|3|sig", "class": "prometedora",
                      "name": "Movimiento de los primeros minutos", "oos": {"trades": 278, "mean_R": 0.1402, "win_rate": 0.518}}],
       "robust": [], "avance": {"days": 668, "trades": 1356, "total_pct": -61.1654, "max_dd_pct": 65.2753,
                                "desde": "2024-04-01", "top": 10, "periodo": "quarter"}}
BT_IMP = {"impulso": {"nota": "Variante de 7 días elegida DESPUÉS de ver los resultados.",
                      "fuera": {"n": 202, "mean_R": 0.3459, "win_rate": 0.4109, "salidas": {"stop": 113, "objetivo": 60},
                                "desde": 1_755_655_200_000, "hasta": 1_790_778_780_000, "total_R": 69.879, "max_dd_R": 22.81},
                      "dentro": {"n": 469, "mean_R": 0.1297, "win_rate": 0.3326}},
          "seis": {"nombre": "6 diarias", "nota": "Réplica del motor: pierde dentro y fuera de muestra.",
                   "fuera": {"n": 1538, "mean_R": -0.12, "win_rate": 0.3524, "salidas": {"stop": 865, "tiempo": 482},
                             "desde": 1_755_476_100_000, "hasta": 1_792_505_400_000, "total_R": -184.596, "max_dd_R": 235.29},
                   "dentro": {"n": 2536, "mean_R": -0.1112, "win_rate": 0.3289}},
          "lab": LAB}


def test_estrategia_6_diarias_honesta_y_paper(radar, monkeypatch):
    monkeypatch.setattr(radar, "get", falso({"/api/aperturas": OPEN_SEIS, "/api/backtest/impulso": BT_IMP}))
    out = radar.main(["estrategia"])
    assert "Estrategia activa del motor de aperturas (cartera paper): 6 diarias (preset «seis»)" in out
    assert "Origen de los parámetros: decisión del usuario (scripts/activar_estrategia.py); cargados el 28-10 13:49 UTC " \
           "(versión 3f2a1b2c, registro v12)" in out
    assert "En marcha: no está pausada" in out and "2 de 6 entradas y 1,00 % de 3,00 % de riesgo" in out
    assert "(cualquier movimiento) y entra a +15 min de la apertura" in out and "sin filtro de volumen" in out
    assert "stop 1,5 % y objetivo 3,0R (+4,5 %), como mucho 24 h; sin break-even ni stop dinámico" in out
    assert "Largos al contado y cortos en perpetuo" in out and "grupo fiable" in out
    assert "«6 diarias» pierde, -0,12 R por operación fuera de muestra (1538 operaciones)" in out
    assert "paper" in out and "No es una promesa de rentabilidad ni un consejo de inversión" in out
    monkeypatch.setattr(radar, "get", falso({"/api/aperturas": OPEN_SEIS}))   # sin backtest en este bot
    assert "unos -0,12 R por operación fuera de muestra" in radar.main(["estrategia"])


def test_estrategia_e2_pausada_por_la_evolucion(radar, monkeypatch):
    a = {**OPEN_SEIS, "params_source": "evolucion", "paused": True, "notice": "Campeón pausado por la evolución",
         "params": {**OPEN_SEIS["params"], "preset": "impulso", "nombre": "E2 · impulso", "max_hold_h": 168,
                    "sl_pct": 3.0, "min_move_pct": 0.2, "entry": "t:30", "rvol_min": 1.5, "btc_filter": "a_favor",
                    "news_mode": "no_contra"},
         "estrategia_cargada": {"firma": "abc", "ts": 1, "pausada": True, "fuente": "evolucion",
                                "motivo": "R contraído -0,4 con 30 operaciones"}}
    monkeypatch.setattr(radar, "get", falso({"/api/aperturas": a, "/api/backtest/impulso": BT_IMP}))
    out = radar.main(["estrategia"])
    assert "E2 · impulso (preset «impulso»)" in out and "la evolución (estrategia_activa.json)" in out
    assert "PAUSADA: el motor mide pero no abre entradas nuevas" in out and "motivo: R contraído -0,4" in out
    assert "si pasa de ±0,2 %" in out and "volumen relativo ≥ 1,5" in out and "solo a favor de la tendencia de BTC" in out
    assert "sin tope de tiempo (7 días de seguridad)" in out and "objetivo 3,0R (+9,0 %)" in out
    assert "+0,14 R por operación fuera de muestra con la salida del laboratorio (278 operaciones)" in out
    assert "+0,35 R con la salida de 7 días" in out and "no está validada" in out
    monkeypatch.setattr(radar, "get", falso({}))
    assert "aún no está en marcha" in radar.main(["estrategia"])


def test_aperturas_horquilla_pura_con_plan_e_instrumento(radar, monkeypatch):
    monkeypatch.setattr(radar, "get", falso({"/api/aperturas": OPEN_SEIS}))
    out = radar.main(["aperturas"])
    assert "→ stop dinámico" not in out and "reentradas" not in out
    assert "horquilla pura (stop y objetivo fijos; sin break-even ni stop dinámico)" in out
    assert "6 diarias (preset «seis»)" in out and "decisión del usuario" in out and "En marcha" in out
    assert "- Europa · en posición" in out and "Se sigue el impulso de los 15 primeros min" in out
    assert "  BTC: en posición, largo, al contado, rvol 3,0, entradas 1 · Impulso de +0,4 % en 15 min" in out
    assert "    plan: entrada 100,0000, stop 98,5000 (1,5 %), objetivo 104,5000 (3,0R), neto +4,26 % tras costes de 0,24 %" in out
    assert "  ETH: terminada, corto, en perpetuo, entradas 0 · Sin entrada" in out
    monkeypatch.setattr(radar, "get", falso({}))
    assert radar.main(["aperturas"]) == "El motor de aperturas aún no está en marcha en el bot."


def test_backtest_lee_el_del_motor_y_el_laboratorio(radar, monkeypatch):
    llamadas = []
    monkeypatch.setattr(radar, "get", falso({"/api/backtest/impulso": BT_IMP, "/api/aperturas": OPEN_SEIS,
                                             "/api/backtest?curvas=0": {"strategies": []}}, llamadas))
    out = radar.main(["backtest"])
    assert "/api/backtest/impulso" in llamadas and "/api/backtest/aperturas" not in llamadas
    assert out.index("«6 diarias»") < out.index("E2 · impulso con la salida de 7 días")   # primero la que opera
    assert "«6 diarias» (preset seis" in out and "← la que opera ahora el paper" in out
    assert "fuera de muestra (18-08-2025 a 20-10-2026): 1538 operaciones, -0,12 R de media, ganadoras 35 %" in out
    assert "R acumulado -184,6 R (caída máxima 235,3 R); salidas: stop 865, tiempo 482" in out
    assert "dentro de muestra: 2536 operaciones, -0,11 R de media" in out and "nota: Réplica del motor" in out
    assert "202 operaciones, +0,35 R de media" in out and "elegida DESPUÉS de ver los resultados" in out
    assert "Laboratorio (2023-11-01 a 2026-10-23, fuera de muestra desde 2025-08-16): 2000 estrategias probadas, " \
           "2 prometedoras y 0 robustas" in out
    assert "prometedora: Movimiento de los primeros minutos · fuera de muestra 278 operaciones, +0,14 R" in out
    assert "Cuenta honesta en avance" in out and "-61,2 % en 668 días" in out
    assert "Estrategias generales: el backtest aún no se ha generado." in out
    assert "ninguna estrategia tiene ventaja robusta" in out and "consejo de inversión" in out
    monkeypatch.setattr(radar, "get", falso({"/api/backtest?curvas=0": {"strategies": []}}))
    assert "Backtest del motor de aperturas: aún no está en este bot" in radar.main(["backtest"])


VIG = {"type": "vigilancia", "estado": "ok", "nota": None, "desde_dia": "2026-10-27", "congelada": "2026-10-26",
       "aperturas_evaluadas": 12, "proxima": {"nombre": "Tokio y Seúl", "evalua_ms": 1_793_250_000_000},
       "estrategias": [{"codigo": "E2-7d", "nombre": "Movimiento de los primeros minutos (7 días)", "clase": "variante",
                        "variante_de": "ap_mom:…|sig", "en_paper": True,
                        "reglas": {"sl_pct": 3.0, "r": 3.0, "dur_texto": "7 días de seguridad"},
                        "vivo": {"operaciones": 6, "abiertas": 1, "R_medio": 0.51, "ganadoras": 0.5, "ic95": [-1.1, 2.1]},
                        "backtest": {"operaciones": 202, "R_medio": 0.3459}}]}


def test_vigilancia_virtual_frente_al_backtest(radar, monkeypatch):
    monkeypatch.setattr(radar, "get", falso({"/api/vigilancia": VIG}))
    out = radar.main(["vigilancia"])
    assert "operaciones VIRTUALES (sin dinero, ni siquiera ficticio). Estado: al día." in out
    assert "Cuenta desde el 2026-10-27; lista congelada el 2026-10-26; 12 aperturas evaluadas; próxima: Tokio y Seúl" in out
    assert "- E2-7d (variante del usuario: sin tope, solo los 7 días de seguridad, la que reproduce el paper)" in out
    assert "en vivo: 6 cerradas, R medio +0,51 R (IC 95 % -1,10 a 2,10 R), ganadoras 50 %, abiertas 1" in out
    assert "backtest fuera de muestra: 202 operaciones, R medio +0,35 R · stop 3,0 %, 3,0R, 7 días de seguridad" in out
    assert "no dice nada" in out
    monkeypatch.setattr(radar, "get", falso({"/api/vigilancia": {"estado": "apagada", "estrategias": []}}))
    assert "apagada" in radar.main(["vigilancia"])


FIA = {"type": "noticias_fiables", "desde": "2026-10-25", "hasta": "2026-10-27", "dias": 3, "historias": 1763,
       "probados": 1649, "con_muestra": 56, "pasan": 6, "operan": 0, "por_azar": 3.39, "p_azar": 0.1118,
       "azar_bloques": {"media": 3.79, "p95": 7, "max": 10, "p": 0.1856},
       "nota": "6 de 56 grupos con la muestra mínima pasan…",
       "grupos": [{"clave": "medio:rbc.ru", "tipo": "medio", "nombre": "rbc.ru", "sentido": None, "n": 36,
                   "n_historias": 9, "dias": 3, "acierto": 0.7222, "acierto_bruto": 0.5, "estado": "fiable", "opera": False,
                   "no_opera": ["9 noticias distintas (hacen falta 30)"]},
                  {"clave": "medio:x.com", "nombre": "x.com", "tipo": "medio", "estado": "en_prueba", "acierto": 0.8}]}


def test_fiables_cuantos_pasan_operan_y_el_azar(radar, monkeypatch):
    monkeypatch.setattr(radar, "get", falso({"/api/noticias_fiables": FIA}))
    out = radar.main(["fiables"])
    assert "3 días con datos (2026-10-25 a 2026-10-27), 1763 noticias distintas" in out
    assert "Pasan el criterio 6 de 56 grupos con la muestra mínima (1649 probados)" in out
    assert "pasarían unos 3,8 (p = 0,186, barajando dentro de cada hora)" in out and "3,4 (p = 0,112)" in out
    assert "no se distinguen del azar" in out and "Pasan la puerta para operar: 0." in out and "no abre nada" in out
    assert "Nota: 6 de 56 grupos" in out
    assert "- rbc.ru (medio): acierto 72 % (sin ajustar 50 %), 36 casos de 9 noticias distintas en 3 días; " \
           "no opera: 9 noticias distintas (hacen falta 30)" in out
    assert "x.com" not in out and "consejo de inversión" in out
    monkeypatch.setattr(radar, "get", falso({}))
    assert "Aún no hay cálculo de noticias fiables" in radar.main(["fiables"])


MEDIO = {"medio": "reuters.com", "nombre": "Reuters", "nivel": "T1", "casos": 120.0, "independientes": 40, "dias": 14,
         "acierto": 0.55, "ic90": [0.48, 0.61], "bruto": 0.53, "base": 0.5, "peso": 1.2, "peso_manual": None,
         "veredicto": "acierta"}
APR = {"type": "aprendizaje", "estado": "ok", "nota": None, "aviso": "Cada hora con noticias es UN caso.",
       "params": {"min_casos": 20, "min_dias": 10},
       "totales": {"historias": 900, "filas": 3000, "completas": 2500, "pendientes": 400, "sin_datos": 100, "medios": 80,
                   "medios_con_datos": 3, "postmortems": {"hechos": 1, "pendientes": 1, "error": 0}},
       "global": {"acierto": 0.49, "ic90": [0.46, 0.52], "bruto": 0.5, "base": 0.5, "casos": 2500.0, "independientes": 300},
       "medios": [MEDIO], "medios_mejores": [MEDIO],
       "medios_peores": [{**MEDIO, "medio": "malo.com", "nombre": "Malo", "acierto": 0.4, "peso": 0.6, "veredicto": "falla"}],
       "postmortems": [{"origen": "paper", "moneda": "SOL", "cerrada": 1_793_190_000_000, "R": -1.0, "estado": "hecho",
                        "texto": "Tocó el stop: la noticia ya estaba descontada.", "resumen": "Stop"},
                       {"origen": "vigilancia", "moneda": "XRP", "cerrada": 1_793_100_000_000, "R": 2.9,
                        "estado": "pendiente", "texto": None, "resumen": "Objetivo a las 9 h"}]}


def test_aprendizaje_ranking_de_medios_y_postmortems(radar, monkeypatch):
    monkeypatch.setattr(radar, "get", falso({"/api/aprendizaje": APR}))
    out = radar.main(["aprendizaje"])
    assert "no opera nada): al día." in out and "2500 de 3000 filas completas" in out and "3 de 80 medios" in out
    assert "Todas juntas: acierto 49 % ajustado al mercado (IC 90 % 46-52 %; sin ajustar 50 %, base 50 %)" in out
    assert "- Reuters (reuters.com, T1): acierto 55 % ajustado al mercado (IC 90 % 48-61 %; sin ajustar 53 %, base 50 %), " \
           "120 casos (40 independientes) en 14 días, peso 1,20, acierta" in out
    assert "peor resultado" in out and "Malo (malo.com, T1): acierto 40 %" in out and "falla" in out
    assert "Post-mortems de las operaciones cerradas: 1 hechos, 1 pendientes, 0 con error" in out
    assert "· paper · SOL · -1,00 R · explicado: Tocó el stop" in out
    assert "· vigilancia (virtual) · XRP · +2,90 R · explicación de la IA pendiente: Objetivo a las 9 h" in out
    assert "aprendizaje --por region" in out and "Cada hora con noticias es UN caso." in out
    sin = {**APR, "medios_mejores": [], "medios_peores": [], "totales": {**APR["totales"], "medios_con_datos": 0}}
    monkeypatch.setattr(radar, "get", falso({"/api/aprendizaje": sin}))
    assert "Ningún medio tiene aún 20 casos independientes en 10 días" in radar.main(["aprendizaje"])
    monkeypatch.setattr(radar, "get", falso({"/api/aprendizaje": {"estado": "apagado"}}))
    assert "apagado" in radar.main(["aprendizaje"])


def test_aprendizaje_por_dimension(radar, monkeypatch):
    est = [{"clave": "asia", "publicaciones": 50.0,
            "principal": {"casos": 80.0, "independientes": 30, "dias": 12, "p_aj": 0.55, "ic90_aj": [0.48, 0.61],
                          "bruto": 0.54, "base": 0.51}}]
    monkeypatch.setattr(radar, "get", falso({"/api/aprendizaje/estadisticas?por=region": est}))
    out = radar.main(["aprendizaje", "--por", "region"])
    assert "Acierto de las noticias por región" in out
    assert "- asia: acierto 55 % (IC 90 % 48-61 %; sin ajustar 54 %, base 51 %), 80 casos (30 independientes) en 12 días" in out
    monkeypatch.setattr(radar, "get", falso({}))
    assert "no está en marcha" in radar.main(["aprendizaje", "--por", "region"])
    assert radar.main(["aprendizaje", "--por", "pais"]).startswith("--por debe ser uno de: nivel, region")


def test_postmortems_todos_o_de_un_origen(radar, monkeypatch):
    rutas = {"/api/aprendizaje/postmortems?limit=10": APR["postmortems"],
             "/api/aprendizaje/postmortems?limit=10&origen=paper": APR["postmortems"][:1]}
    monkeypatch.setattr(radar, "get", falso(rutas))
    out = radar.main(["postmortems"])
    assert "Post-mortems (de la más reciente" in out and "SOL" in out and "XRP" in out and "paper" in out
    out = radar.main(["postmortems", "--origen", "paper"])
    assert "Post-mortems de la cartera paper" in out and "XRP" not in out
    assert radar.main(["postmortems", "--origen", "web"]).startswith("--origen debe ser uno de")
    monkeypatch.setattr(radar, "get", falso({"/api/aprendizaje/postmortems?limit=10": []}))
    assert radar.main(["postmortems"]) == "Aún no hay post-mortems."


EVO = {"type": "evolucion", "estado": "ok", "nota": None, "modo": "proponer", "aviso": "Solo paper.",
       "campeon": {"nombre": "6 diarias", "codigo": None, "fuente": "usuario", "pausada": False, "desde": 1_793_195_340_000,
                   "confirmado": True, "pendiente_reinicio": False, "en_curso": 1,
                   "vivo": {"operaciones": 3, "R_medio": -0.2, "ganadoras": 0.33, "ic95": None, "muestra": "Muestra pequeña."},
                   "gemelo": {"codigo": "E2-7d"}, "virtual": {"operaciones": 6, "R_medio": 0.51},
                   "degradacion": {"evaluada": False}},
       "aspirantes": [{"id": "ap_mom:…|7d", "codigo": "E1-7d", "clase": "prometedora", "vivo": {"operaciones": 4, "R_medio": 0.8},
                       "frente": {"dif_R": 0.3, "cumple": False, "faltan": ["≥ 40 operaciones (lleva 4)"],
                                  "confirmacion": {"texto": "En observación"}}}],
       "propuestas": [{"id": "p1", "tipo": "parametro_cambiado", "titulo": "Bajar el riesgo", "texto": "Con 30 operaciones…"}],
       "informe": {"semana": "2026-W44", "titulo": "Semana 44", "resumen": "Corto"},
       "informe_semanal": {"semana": "2026-W44", "titulo": "Semana 44",
                           "secciones": [{"titulo": "Resumen", "lineas": ["3 operaciones en paper.", "Sin cambios."]},
                                         {"titulo": "Avisos", "lineas": ["Solo paper."]}]}}


def test_evolucion_campeon_aspirantes_propuestas_e_informe(radar, monkeypatch):
    monkeypatch.setattr(radar, "get", falso({"/api/evolucion": EVO}))
    out = radar.main(["evolucion"])
    assert "Evolución (solo paper; modo proponer: nada se aplica solo): al día." in out
    assert "Campeón (la estrategia que opera el motor): 6 diarias, puesto por el usuario, confirmado por el motor" in out
    assert "En el paper: 3 cerradas, R medio -0,20 R (IC 95 % –), ganadoras 33 %; 1 en curso. Muestra pequeña." in out
    assert "Gemelo virtual en la vigilancia: E2-7d, 6 operaciones con R medio +0,51 R." in out
    assert "- E1-7d (prometedora): 4 operaciones, R medio +0,80 R; frente al campeón +0,30 R; le falta: ≥ 40 operaciones " \
           "(lleva 4); En observación" in out
    assert "Propuestas pendientes: 1:\n" in out and "- Bajar el riesgo (ajuste de parámetros): Con 30 operaciones…" in out
    assert "Informe semanal 2026-W44: Semana 44" in out and "  Resumen: 3 operaciones en paper. · Sin cambios." in out
    assert "esta skill solo las lee" in out and "consejo de inversión" in out
    monkeypatch.setattr(radar, "get", falso({"/api/evolucion": {"estado": "apagada"}}))
    assert "apagada" in radar.main(["evolucion"])


def test_propuestas_de_la_evolucion(radar, monkeypatch):
    ps = {"propuestas": [{"id": "p1", "ts": 1_793_100_000_000, "tipo": "estrategia_degradada", "sujeto": "campeon",
                          "estado": "descartada", "resuelta_por": "web", "nota": "Muestra pequeña",
                          "cuerpo": {"titulo": "Pausar el campeón", "texto": "R contraído -0,4"}}]}
    monkeypatch.setattr(radar, "get", falso({"/api/evolucion/propuestas?limite=15&estado=descartada": ps,
                                             "/api/evolucion/propuestas?limite=15": {"propuestas": []}}))
    out = radar.main(["propuestas", "--estado", "descartada"])
    assert "Propuestas de la evolución en estado «descartada»" in out
    assert "· pausar el campeón · descartada por web: Pausar el campeón. R contraído -0,4 (nota: Muestra pequeña)" in out
    assert radar.main(["propuestas"]) == "No hay propuestas de la evolución."
    assert radar.main(["propuestas", "--estado", "todas"]).startswith("--estado debe ser uno de")
    monkeypatch.setattr(radar, "get", falso({}))
    assert "no está en marcha" in radar.main(["propuestas"])


FV = {"type": "fuentes_vivas", "auto": True, "descubrir": False,
      "feeds": {"activa": 5600, "prueba": 3, "silenciada": 100, "retirada": 11},
      "medios": {"activa": 0, "prueba": 0, "silenciada": 2, "retirada": 0},
      "lista": [{"id": "medio:malo.com", "tipo": "medio", "nombre": "malo.com", "estado": "silenciada",
                 "motivo": "Acierto 38 % en 35 casos", "desde": 1_793_000_000_000, "manual": False}],
      "candidatos": [{"medio": "nuevo.jp", "nombre": "Nuevo", "estado": "verificado",
                      "estado_texto": "RSS verificado: pendiente de que lo añadas", "rss": "https://nuevo.jp/rss", "nota": None},
                     {"medio": "otro.kr", "nombre": None, "estado": "sin_rss", "estado_texto": "Sin RSS válido"}],
      "cambios": [{"ts": 1_793_000_000_000, "id": "medio:malo.com", "nombre": "malo.com", "texto": "Silenciada",
                   "motivo": "Acierto 38 %", "auto": True}]}


def test_fuentes_silenciadas_candidatos_y_cambios(radar, monkeypatch):
    monkeypatch.setattr(radar, "get", falso({"/api/fuentes_vivas?estado=silenciada&limite=15": FV}))
    out = radar.main(["fuentes"])
    assert "Revisión automática encendida; descubrimiento de medios apagado." in out
    assert "Feeds: 5600 activos, 3 en prueba, 100 silenciados, 11 retirados." in out
    assert "Medios: 0 activos, 0 en prueba, 2 silenciados, 0 retirados." in out
    assert "Silenciadas (hasta 15):\n- malo.com (medio): Acierto 38 % en 35 casos, desde el" in out
    assert "1 con RSS verificado, pendientes de que los añadas, 1 sin RSS válido" in out
    assert "- Nuevo (nuevo.jp): RSS verificado: pendiente de que lo añadas · RSS https://nuevo.jp/rss" in out
    assert "· Silenciada: malo.com (Acierto 38 %) · automático" in out and "esta skill solo lee" in out
    monkeypatch.setattr(radar, "get", falso({"/api/fuentes_vivas?estado=silenciada&limite=15": {"estado": "apagado"}}))
    assert "no están en marcha" in radar.main(["fuentes"])


def _instalar():
    spec = importlib.util.spec_from_file_location("instalar_openclaw", INSTALAR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_instalar_permite_justo_los_comandos_de_la_skill(radar):
    import re
    ins = _instalar()
    allow = ins.approvals()["agents"]["trading"]["allowlist"]
    assert all(a["pattern"] == ins.PY for a in allow)

    def permitido(cmd: str) -> bool:
        return any(re.search(a["argPattern"], f"-I -B {RADAR_WIN} {cmd}") for a in allow)

    # Los mismos comandos y opciones en la skill y en el instalador
    assert set(ins.SIMPLES) == (set(radar.SIMPLES) | set(radar.CON_OPCIONES) | set(radar.DE_ESTADO)) - {"noticias"}
    assert ins.APR_POR == radar.APR_POR and ins.PM_ORIGEN == radar.PM_ORIGEN
    assert ins.PROP_ESTADOS == radar.PROP_ESTADOS and ins.BOLSAS == radar.BOLSAS
    for cmd in (*ins.SIMPLES, "noticias", "noticias --horas 3 --min 5", "noticias --horas 48 --min 10 --bolsa tokio",
                *(f"aprendizaje --por {p}" for p in ins.APR_POR), "postmortems --origen paper",
                "postmortems --origen vigilancia", *(f"propuestas --estado {e}" for e in ins.PROP_ESTADOS)):
        assert permitido(cmd), cmd
    for cmd in ("estado; del x", "fiables && calc", "borrar", "aprendizaje --por pais", "aprendizaje --por region --x",
                "postmortems --origen web", "propuestas --estado todas", "noticias --horas 49", "noticias --bolsa paris",
                "evolucion --aplicar p1", "fuentes --silenciar medio:x.com", " estado", "estrategia x"):
        assert not permitido(cmd), cmd
    assert not any(re.search(a["argPattern"], f"-I -B C:\\otro\\radar.py estado") for a in allow)


def test_skill_md_y_agents_md_listan_todos_los_comandos():
    ins = _instalar()
    skill, agents = SKILL_MD.read_text(encoding="utf-8"), AGENTS_MD.read_text(encoding="utf-8")
    for cmd in (*ins.SIMPLES, "noticias"):
        assert f"`{cmd}" in skill, cmd
        assert f"radar.py {cmd}" in agents, cmd
    for x in ("aprendizaje --por region", "postmortems --origen paper", "propuestas --estado pendiente"):
        assert x in skill and x.split()[0] in agents
    for t in (skill, agents):
        assert "paper" in t and "−0,12 R" in t and "consejos de inversión" in t and "stop dinámico →" not in t
    assert "sin break-even ni stop dinámico" in skill and "reentradas" not in skill


def test_sin_bot_dice_que_no_responde_y_no_que_faltan_datos(radar, monkeypatch):
    def caido(path):
        raise urllib.error.URLError(ConnectionRefusedError(10061, "rechazada"))
    monkeypatch.setattr(radar, "get", caido)
    for cmd in ("backtest", "estrategia", "aperturas", "vigilancia", "fiables", "aprendizaje", "evolucion", "fuentes",
                "postmortems", "propuestas"):
        with pytest.raises(urllib.error.URLError):   # el __main__ lo imprime como «ERROR: el bot no responde»
            radar.main([cmd])
