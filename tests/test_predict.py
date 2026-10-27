import json
import math
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bot import backtest, predict
from bot.bus import Bus
from bot.predict import BacktestIndex, Predictor, combine, confidence, flow_effect, news_pressure, reliability
from bot.store import Store

UTC = timezone.utc
H = 3600_000
TEST_TMP = Path(os.environ.get("BOT_TEST_TMP", r"M:\BotFundamental-runtime\tmp-pruebas"))
NOW = datetime(2026, 10, 21, 6, 45, tzinfo=UTC)     # miércoles: Tokio y Seúl ya cerraron, Fráncfort abre a las 07:00
NOW_CR = datetime(2026, 10, 21, 6, 30, tzinfo=UTC)  # media hora antes de la apertura de Londres
ITEM_KEYS = {"id", "kind", "target", "label", "event", "when_utc", "horizon", "prob_up", "expected_pct", "confidence",
             "n_samples", "drivers", "note"}


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def logit(p: float) -> float:
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


@pytest.fixture
def mtmp():
    """Carpeta temporal en M: (los datos nunca van a C:); si no hay M:, la del sistema."""
    try:
        TEST_TMP.mkdir(parents=True, exist_ok=True)
        d = Path(tempfile.mkdtemp(dir=TEST_TMP))
    except OSError:
        d = Path(tempfile.mkdtemp())
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def store(mtmp):
    s = Store(mtmp / "bot.db")
    yield s
    s.close()


# ---- Backtest de ejemplo, con la forma de resultados.json (contrato 3.1), recortado

def strat(sid, family, n, hit, base, p, verdict, actionable=True, **params):
    return {"id": sid, "family": family, "name": sid, "description": "", "n": n, "hit_rate": hit, "base_rate": base,
            "lift_pp": round((hit - base) * 100, 2), "p_value": p, "avg_ret_pct": 0.05, "total_ret_pct": 10.0,
            "win_rate": hit, "profit_factor": 1.2, "max_dd_pct": 8.0, "sharpe": 0.5, "trades_per_year": 100.0,
            "actionable": actionable, "oos": {"n": n // 3, "hit_rate": hit, "avg_ret_pct": 0.04}, "verdict": verdict,
            "verdict_why": "", "equity": [], "params": params}


def op(a, lag, b, up, n, hit, base, p, verdict):
    leg = f"{a}_ayer" if lag else a
    return strat(f"encadenamiento-{leg}-{b}-{'compra' if up else 'venta'}", "encadenamiento", n, hit, base, p, verdict,
                 tipo="operable", legs=[[a, lag]], b=b, direccion="compra" if up else "venta")


LL_MARKETS = ["tokio", "seul", "frankfurt", "londres", "nuevayork"]


def matrix(diag, off):
    return [[diag if i == j else off for j in range(len(LL_MARKETS))] for i in range(len(LL_MARKETS))]


BACKTEST = {
    "generated": 1, "data": {},
    "strategies": [
        op("tokio", 0, "frankfurt", True, 600, 0.60, 0.52, 1e-4, "funciona"),
        op("tokio", 0, "frankfurt", False, 500, 0.50, 0.48, 0.3, "no_funciona"),
        op("nuevayork", 1, "frankfurt", True, 700, 0.56, 0.52, 0.01, "dudosa"),
        # Relaciones de cierre a cierre: solo valen para índices sin apertura fiable (Londres). Si pasan todas las
        # pruebas, el backtest las deja en «solo_correlacion» (nunca «funciona», porque no se pueden operar).
        strat("encadenamiento-tokio-londres-sube", "encadenamiento", 1200, 0.60, 0.535, 1e-6, "solo_correlacion",
              actionable=False, tipo="relacion", a="tokio", b="londres", lag=0, direccion="sube"),
        strat("encadenamiento-seul-londres-sube", "encadenamiento", 1200, 0.60, 0.535, 1e-6, "dudosa",
              tipo="relacion", a="seul", b="londres", lag=0, direccion="sube"),
        strat("encadenamiento-seul-frankfurt-sube", "encadenamiento", 1200, 0.62, 0.53, 1e-8, "solo_correlacion",
              tipo="relacion", a="seul", b="frankfurt", lag=0, direccion="sube"),
        strat("bolsa_a_cripto-tokio-btc-londres-compra", "bolsa_a_cripto", 1100, 0.53, 0.49, 0.004, "dudosa",
              senal="tokio", ventana="londres", direccion="compra", salida="4 h"),
        strat("bolsa_a_cripto-tokio-btc-londres-venta", "bolsa_a_cripto", 1000, 0.52, 0.50, 0.2, "no_funciona",
              senal="tokio", ventana="londres", direccion="venta", salida="4 h"),
        strat("ventana_cripto-londres-tendencia-btc", "ventana_cripto", 1500, 0.53, 0.49, 0.001, "dudosa",
              ventana="londres", tipo="tendencia", salida="2 h"),
    ],
    "leadlag": {"markets": LL_MARKETS, "base_up": [0.53, 0.55, 0.53, 0.535, 0.546],
                "p_up_given_up": matrix(1.0, 0.58), "p_down_given_down": matrix(1.0, 0.52),
                "n": matrix(2400, 2300), "actionable": matrix(False, True),
                "avg_ret_up_pct": matrix(0.9, 0.5), "avg_ret_down_pct": matrix(-0.9, -0.5),
                "lag1": {"p_up_given_up": matrix(0.5, 0.60), "p_down_given_down": matrix(0.5, 0.55),
                         "n": matrix(2400, 2300)}}}


def ls(date: str, ch: float, close: float = 100.0) -> dict:
    return {"last_session": {"date": date, "change_pct": ch, "close": close}}


def make(store, trend=None, of=None, market=None, bt=BACKTEST):
    bus = Bus(store)
    box = {"trend": trend, "of": of, "market": market, "bt": bt}
    p = Predictor(bus, store, lambda: box["trend"], lambda: box["of"], lambda: box["market"], lambda: box["bt"])
    return p, bus, box


def item(msg, pid):
    return next(i for i in msg["items"] if i["id"] == pid)


def news(bus, story, ts, rep, cred=80, resumen="Titular"):
    bus.publish({"type": "event", "kind": "triaged", "agent": "local", "story": story, "ts": ts, "title": resumen,
                 "coins": [], "text": "", "patch": {"triage": {"credibilidad": cred, "repercusion": rep, "resumen": resumen,
                                                                "importancia": 7}}})


# ---- Combinación de factores

def test_combina_en_log_odds_y_reparte_el_efecto():
    prob, drv = combine(0.5, [{"factor": "a", "raw_pp": 10, "source": ""}, {"factor": "b", "raw_pp": 10, "source": ""}])
    assert prob == pytest.approx(sigmoid(2 * logit(0.6)), abs=1e-9)          # 0,692, no 0,70: se suma en log-odds
    assert sum(d["effect_pp"] for d in drv) == pytest.approx((prob - 0.5) * 100, abs=0.02)
    assert drv[0]["effect_pp"] == drv[1]["effect_pp"] and "raw_pp" not in drv[0]
    # Un solo factor mueve la tasa base exactamente lo que dice.
    prob, drv = combine(0.52, [{"factor": "a", "raw_pp": 6.0, "source": ""}])
    assert prob == pytest.approx(0.58) and drv[0]["effect_pp"] == pytest.approx(6.0, abs=0.01)
    # Un grupo de señales parecidas se encoge con 1/√k.
    prob, _ = combine(0.5, [{"factor": "a", "raw_pp": 10, "group": "g", "source": ""},
                            {"factor": "b", "raw_pp": 10, "group": "g", "source": ""}])
    assert prob == pytest.approx(sigmoid(2 * logit(0.6) / math.sqrt(2)), abs=1e-9)
    # Sin factores, la tasa base; con efectos contrarios, se compensan.
    assert combine(0.53, [])[0] == pytest.approx(0.53)
    prob, drv = combine(0.5, [{"factor": "a", "raw_pp": 8, "source": ""}, {"factor": "b", "raw_pp": -8, "source": ""}])
    assert prob == pytest.approx(0.5) and drv[0]["effect_pp"] == -drv[1]["effect_pp"]


def test_recorta_la_probabilidad():
    big = [{"factor": str(i), "raw_pp": 45, "source": ""} for i in range(3)]
    prob, drv = combine(0.5, big)
    assert prob == 0.95 and sum(d["effect_pp"] for d in drv) == pytest.approx(45, abs=0.02)
    prob, _ = combine(0.5, [{**d, "raw_pp": -45} for d in big])
    assert prob == 0.05


def test_fiabilidad_por_n_p_y_veredicto():
    s = strat("x", "encadenamiento", 600, 0.6, 0.52, 1e-4, "funciona")
    assert reliability(s) == pytest.approx(600 / 750 * (1 - 1e-4 / 0.1))
    assert reliability({**s, "verdict": "no_funciona"}) == 0
    assert reliability({**s, "actionable": False}) == 0
    assert reliability({**s, "p_value": 0.2}) == 0
    assert reliability({**s, "verdict": "dudosa"}) < reliability(s)
    assert reliability({**s, "n": 3000}) > reliability(s) > reliability({**s, "n": 100})
    # Una relación firme de cierre a cierre pesa como «funciona»; una dudosa, menos.
    assert reliability({**s, "verdict": "solo_correlacion"}) == reliability(s)
    assert reliability({**s, "verdict": "dudosa"}) == pytest.approx(0.6 * reliability({**s, "verdict": "solo_correlacion"}))


def test_confianza():
    assert confidence(2400, [6.0, 3.0], 0.62) == "alta"
    assert confidence(2400, [6.0, -5.0], 0.52) == "baja"           # se contradicen
    assert confidence(2400, [4.0], 0.57) == "media"
    assert confidence(0, [4.0], 0.56) == "baja"                    # sin historia y un solo factor


def test_flujo_y_noticias():
    raw, text = flow_effect({"imbalance": 0.5, "cvd_15m": 30, "vol_5m_usd": 3e6, "price": 100000, "trend": "alcista"})
    assert raw == pytest.approx(6.0) and "alcista" in text
    raw, _ = flow_effect({"imbalance": -1.0, "cvd_15m": -300, "vol_5m_usd": 3e6, "price": 100000, "trend": "bajista"})
    assert raw == pytest.approx(-6.0)
    assert flow_effect({"imbalance": None, "trend": None}) is None
    items = [(10, {"credibilidad": 80, "repercusion": {"direccion": -60, "magnitud": 50, "bolsas": ["frankfurt"]},
                   "resumen": "Fuerte"}),
             (10, {"credibilidad": 50, "repercusion": {"direccion": 20, "magnitud": 30, "bolsas": ["frankfurt"]},
                   "resumen": "Débil"}),
             (0, {"credibilidad": 90, "repercusion": {"direccion": 90, "magnitud": 90, "bolsas": ["frankfurt"]}})]
    raw, k, top = news_pressure(items, lambda r: "frankfurt" in r["bolsas"], 5)
    assert (raw, k, top[1]) == (-10.0, 2, "Fuerte")               # −24 + 3 → tope de −10; la de ts 0 queda fuera


# ---- Bolsas

def test_bolsa_encadenamiento_y_sesion_anterior_de_nueva_york(store):
    trend = {"markets": {"tokio": ls("2026-10-21", 1.1), "seul": ls("2026-10-21", 0.7),
                         "shanghai": ls("2026-10-20", 0.2), "nuevayork": ls("2026-10-20", 0.5)}}
    p, bus, _ = make(store, trend)
    it = item(p.run_round(NOW), "frankfurt-2026-10-21")
    assert (it["kind"], it["target"], it["event"], it["horizon"]) == ("bolsa", "frankfurt", "apertura", "sesión")
    assert it["label"] == "Fráncfort (DAX 40)" and it["measure"] == "apertura_a_cierre"
    assert it["when_utc"] == ms(datetime(2026, 10, 21, 7, 0, tzinfo=UTC))
    assert it["ends_utc"] == ms(datetime(2026, 10, 21, 15, 30, tzinfo=UTC))
    assert it["base_rate"] == 0.52 and it["n_samples"] == 2400
    factors = {d["factor"] for d in it["drivers"]}
    assert factors == {"Tokio cerró +1,10 %", "Nueva York cerró +0,50 % en la sesión anterior"}
    # Seúl solo tiene relación de cierre a cierre: no sirve para una sesión de apertura a cierre.
    assert not any("Seúl" in d["factor"] for d in it["drivers"])
    w1, w2 = 600 / 750 * (1 - 1e-4 / 0.1), 700 / 850 * (1 - 0.01 / 0.1) * 0.6
    d1, d2 = (logit(0.52 + 0.08 * w1) - logit(0.52)), (logit(0.52 + 0.04 * w2) - logit(0.52))
    assert it["prob_up"] == pytest.approx(sigmoid(logit(0.52) + (d1 + d2) / math.sqrt(2)), abs=1e-4)
    assert sum(d["effect_pp"] for d in it["drivers"]) == pytest.approx((it["prob_up"] - 0.52) * 100, abs=0.03)
    assert "encadenamiento tokio→frankfurt" in next(d["source"] for d in it["drivers"] if "Tokio" in d["factor"])
    # Shanghái cierra a las 07:00 UTC, justo cuando abre Fráncfort: con los 15 min de margen del backtest no llega a
    # tiempo, así que no se espera. A Nueva York (13:30 UTC) sí le faltan Shanghái y Hong Kong.
    assert "falta el cierre" not in it["note"]
    assert "falta el cierre de Shanghái y Hong Kong" in item(bus.state["predictions"], "nuevayork-2026-10-21")["note"]
    # Tokio abre mañana: Nueva York aún no ha cerrado su sesión de hoy.
    tk = item(bus.state["predictions"], "tokio-2026-10-22")
    assert tk["drivers"] == [] and "falta el cierre de Nueva York" in tk["note"]


def test_cierre_con_margen_como_en_el_backtest(store):
    """En invierno Hong Kong cierra a las 08:00 UTC, cuando abren Fráncfort y Londres: su cierre no se conoce a tiempo
    (subasta hasta las 16:10), ni para la bolsa ni para la ventana cripto de Londres."""
    now = datetime(2026, 11, 4, 7, 30, tzinfo=UTC)
    bt = {**BACKTEST, "strategies": BACKTEST["strategies"] + [
        strat(f"bolsa_a_cripto-{k}-btc-londres-compra", "bolsa_a_cripto", 1100, 0.55, 0.49, 0.004, "dudosa",
              senal=k, ventana="londres", direccion="compra", salida="4 h") for k in ("shanghai", "hongkong")]}
    trend = {"markets": {"shanghai": ls("2026-11-04", 0.5), "hongkong": ls("2026-11-04", 1.0)}}
    p, _, _ = make(store, trend, bt=bt)
    msg = p.run_round(now)
    fr = item(msg, "frankfurt-2026-11-04")
    assert fr["when_utc"] == ms(datetime(2026, 11, 4, 8, 0, tzinfo=UTC)) and "Hong Kong" not in fr["note"]
    w = item(msg, "BTC-londres-2026-11-04")
    factors = {d["factor"] for d in w["drivers"]}
    assert "Shanghái cerró +0,50 %" in factors and not any("Hong Kong" in f for f in factors)


def test_bolsa_peso_cero_si_no_funciona(store):
    p, _, _ = make(store, {"markets": {"tokio": ls("2026-10-21", -1.1)}})
    it = item(p.run_round(NOW), "frankfurt-2026-10-21")
    assert it["drivers"] == [] and it["prob_up"] == 0.52
    assert "sin peso según el backtest: Tokio (no funciona)" in it["note"]


def test_bolsa_sin_apertura_fiable_va_de_cierre_a_cierre(store):
    trend = {"markets": {"tokio": ls("2026-10-21", 1.1), "seul": ls("2026-10-21", 0.7), "nuevayork": ls("2026-10-20", 0.5)}}
    p, _, _ = make(store, trend)
    it = item(p.run_round(NOW), "londres-2026-10-21")
    assert it["measure"] == "cierre_a_cierre" and it["base_rate"] == 0.535
    src = {d["factor"]: d["source"] for d in it["drivers"]}
    eff = {d["factor"]: d["effect_pp"] for d in it["drivers"]}
    # La relación de Tokio se usa aunque el backtest la marcara no operable ese día: aquí se comprueba con el horario.
    # Es «solo_correlacion» (firme pero no operable): para predecir de cierre a cierre pesa entera.
    assert "relación de cierre a cierre" in src["Tokio cerró +1,10 %"] and "solo correlación" in src["Tokio cerró +1,10 %"]
    assert "leadlag" in src["Nueva York cerró +0,50 % en la sesión anterior"]
    assert "sin ventaja" not in it["note"]
    # Seúl tiene la misma relación pero «dudosa»: pesa el 60 %.
    w = 1200 / 1350 * (1 - 1e-6 / 0.1)
    d_t, d_s = logit(0.535 + 0.065 * w) - logit(0.535), logit(0.535 + 0.065 * w * 0.6) - logit(0.535)
    assert "dudosa" in src["Seúl cerró +0,70 %"]
    assert eff["Seúl cerró +0,70 %"] / eff["Tokio cerró +1,10 %"] == pytest.approx(d_s / d_t, rel=0.01)
    assert it["prob_up"] > 0.535


def test_bolsa_noticias_con_tope_y_12_horas(store):
    p, bus, _ = make(store)
    t = ms(NOW)
    news(bus, "n1", t - H, {"direccion": -60, "magnitud": 50, "activos": [], "bolsas": ["frankfurt", "madrid"]},
         resumen="Alemania anuncia aranceles")
    news(bus, "n2", t - H, {"direccion": 20, "magnitud": 30, "activos": [], "bolsas": ["tokio"]}, cred=50)
    news(bus, "n3", t - 13 * H, {"direccion": 90, "magnitud": 90, "activos": [], "bolsas": ["frankfurt"]})
    it = item(p.run_round(NOW), "frankfurt-2026-10-21")
    assert [d["factor"] for d in it["drivers"]] == ["1 noticia sobre Fráncfort (12 h)"]
    assert it["prob_up"] == pytest.approx(0.42) and "aranceles" in it["drivers"][0]["source"]
    tk = item(bus.state["predictions"], "tokio-2026-10-22")
    assert tk["drivers"][0]["effect_pp"] == pytest.approx(20 * 30 * 50 / 1e4, abs=0.01)


def test_bolsa_futuros(store):
    # En este backtest Nueva York no tiene estrategias operables: se mide de cierre a cierre y el futuro cuenta.
    trend = {"markets": {"nuevayork": {"futures": {"symbol": "ES=F", "change_pct": 0.5}, "updated": ms(NOW) - 60_000}}}
    p, _, box = make(store, trend)
    it = item(p.run_round(NOW), "nuevayork-2026-10-21")
    assert it["measure"] == "cierre_a_cierre"
    assert it["drivers"][0]["factor"] == "Futuros ES=F +0,50 %" and "sin validar" in it["drivers"][0]["source"]
    assert it["prob_up"] == pytest.approx(0.546 + 0.025)
    assert it["expected_pct"] == pytest.approx(it["prob_up"] * 0.9 - (1 - it["prob_up"]) * 0.9, abs=1e-3)
    # Un futuro que no se actualiza desde hace 30 min (consulta caída) no se usa.
    box["trend"]["markets"]["nuevayork"]["updated"] = ms(NOW) - 30 * 60_000
    it = item(p.run_round(NOW + timedelta(minutes=5)), "nuevayork-2026-10-21")
    assert it["drivers"] == [] and it["prob_up"] == 0.546


def test_futuros_sin_peso_de_apertura_a_cierre(store):
    """Antes de la apertura el cambio del futuro es casi todo el hueco de apertura, que ya está en el precio de apertura:
    no anticipa el tramo de apertura a cierre."""
    bt = {**BACKTEST, "strategies": BACKTEST["strategies"] + [op("tokio", 0, "nuevayork", True, 600, 0.55, 0.53, 0.02, "dudosa")]}
    trend = {"markets": {"nuevayork": {"futures": {"symbol": "ES=F", "change_pct": 0.5}}}}
    p, _, _ = make(store, trend, bt=bt)
    it = item(p.run_round(NOW), "nuevayork-2026-10-21")
    assert it["measure"] == "apertura_a_cierre" and it["drivers"] == [] and it["prob_up"] == 0.53
    assert "futuros ES=F +0,50 % sin peso" in it["note"]


# ---- Cripto

def crypto_world(store, price=101000.0):
    trend = {"markets": {"tokio": ls("2026-10-21", 1.1), "seul": ls("2026-10-21", 0.4)}}
    of = {"coins": {"BTC": {"imbalance": 0.5, "cvd_15m": 30, "vol_5m_usd": 3e6, "price": 100000, "trend": "alcista"}}}
    market = {"prices": {"BTC": price}, "flow": {"BTC": {"ls": 2.8, "label": "muy largo", "extreme": True}}}
    p, bus, box = make(store, trend, of, market)
    t = ms(NOW_CR)
    p.px["BTC"].extend([(t - 4 * H - 60_000, 100000.0), (t, 101000.0)])
    news(bus, "c1", t - H, {"direccion": 50, "magnitud": 40, "activos": ["BTC"], "bolsas": []}, cred=50,
         resumen="Entrada récord en los ETF de bitcoin")
    return p, bus, box


def test_cripto_ventana_y_proxima_hora(store):
    p, _, _ = crypto_world(store)
    msg = p.run_round(NOW_CR)
    w = item(msg, "BTC-londres-2026-10-21")
    assert (w["kind"], w["event"], w["horizon"], w["label"]) == ("cripto", "ventana", "4h", "BTC · Apertura Londres")
    assert w["when_utc"] == ms(datetime(2026, 10, 21, 7, 0, tzinfo=UTC)) and w["ends_utc"] == w["when_utc"] + 4 * H
    assert w["base_rate"] == 0.49 and w["n_samples"] == 2100
    assert w["note"].startswith("Tasa base 49,0 %: BTC subió en las 4 h siguientes en el 49,0 % de 2100 días del backtest")
    src = next(d["source"] for d in w["drivers"] if "últimas 4 h" in d["factor"])
    assert "a 2 h acierta" in src and "la predicción es a 4 h" in src
    f = {d["factor"]: d["effect_pp"] for d in w["drivers"]}
    assert set(f) == {"Tokio cerró +1,10 %", "BTC +1,00 % en las últimas 4 h", "Minorista muy largo (ratio L/S 2,80)",
                      "1 noticia sobre BTC (6 h)",
                      "Flujo de órdenes: tendencia alcista, desequilibrio +0,50, CVD 15 min +30,0"}
    assert f["Minorista muy largo (ratio L/S 2,80)"] < 0 < f["1 noticia sobre BTC (6 h)"]
    assert sum(f.values()) == pytest.approx((w["prob_up"] - 0.49) * 100, abs=0.05)
    h = item(msg, "BTC-1h-2026-10-21T06:30")
    assert (h["horizon"], h["base_rate"], h["when_utc"]) == ("1h", 0.5, ms(NOW_CR))
    assert len(h["drivers"]) == 3 and h["prob_up"] == pytest.approx(sigmoid(logit(0.56) + logit(0.46) + logit(0.6)), abs=1e-4)
    assert p.refs["BTC-1h-2026-10-21T06:30"] == 101000.0          # la próxima hora se mide desde ahora
    assert "BTC-londres-2026-10-21" not in p.refs                 # la ventana, desde su inicio
    assert {i["target"] for i in msg["items"] if i["kind"] == "cripto"} == {"BTC", "ETH", "SOL", "XRP"}


def test_flujo_pierde_peso_si_la_ventana_queda_lejos(store):
    p, _, _ = crypto_world(store)
    msg = p.run_round(datetime(2026, 10, 21, 0, 0, tzinfo=UTC))   # Londres abre dentro de 7 h
    w = item(msg, "BTC-londres-2026-10-21")
    assert not any(d["factor"].startswith("Flujo") or "últimas 4 h" in d["factor"] for d in w["drivers"])
    h = item(msg, "BTC-1h-2026-10-21T00:00")
    assert any(d["factor"].startswith("Flujo") for d in h["drivers"])


def test_registro_y_evaluacion_cripto(store):
    p, bus, box = crypto_world(store)
    msg = p.run_round(NOW_CR)
    w, h = item(msg, "BTC-londres-2026-10-21"), item(msg, "BTC-1h-2026-10-21T06:30")
    assert w["prob_up"] > 0.5 and h["prob_up"] > 0.5
    rows = store.db.execute("SELECT id, kind, target, when_utc, horizon, prob_up, outcome_pct, hit FROM predictions").fetchall()
    assert len(rows) == len(msg["items"]) and all(r[6] is None and r[7] is None for r in rows)
    t0 = datetime(2026, 10, 21, 7, 0, 5, tzinfo=UTC)
    box["market"] = {"prices": {"BTC": 101000.0}, "flow": {}}   # market publica un mensaje nuevo con cada cambio
    p.tick(t0)                                                   # empieza la ventana: precio de referencia
    assert p.refs["BTC-londres-2026-10-21"] == 101000.0
    box["market"] = {"prices": {"BTC": 102010.0}, "flow": {}}
    p.tick(datetime(2026, 10, 21, 7, 30, 10, tzinfo=UTC))         # pasa la hora: +1 %
    out, hit = store.db.execute("SELECT outcome_pct, hit FROM predictions WHERE id='BTC-1h-2026-10-21T06:30'").fetchone()
    assert out == pytest.approx(1.0) and hit == 1
    box["market"] = {"prices": {"BTC": 100000.0}, "flow": {}}
    p.tick(datetime(2026, 10, 21, 11, 0, 10, tzinfo=UTC))         # acaba la ventana: −0,99 %
    out, hit = store.db.execute("SELECT outcome_pct, hit FROM predictions WHERE id='BTC-londres-2026-10-21'").fetchone()
    assert out == pytest.approx(-0.9901, abs=1e-3) and hit == 0
    # ETH, SOL y XRP no tenían precio: no se evalúan y no cuentan.
    assert store.db.execute("SELECT COUNT(*) FROM predictions WHERE target='ETH' AND hit=-1").fetchone()[0] == 2
    acc = p.accuracy()
    assert acc["evaluated"] == 2 and acc["hit_rate"] == 0.5
    assert acc["by_kind"]["cripto"] == {"evaluated": 2, "hit_rate": 0.5} and acc["by_kind"]["bolsa"]["evaluated"] == 0
    assert set(acc["by_horizon"]) == {"1h", "4h"} and 0 < acc["brier"] < 1
    assert "BTC-londres-2026-10-21" not in p.pending
    assert store.db.execute("SELECT COUNT(*) FROM prediction_refs WHERE id LIKE 'BTC%'").fetchone()[0] == 0
    ev = p.evaluated(10)
    assert {e["id"] for e in ev if e["hit"] >= 0} == {"BTC-londres-2026-10-21", "BTC-1h-2026-10-21T06:30"}
    assert p.evaluated(1)[0]["when_utc"] == max(e["when_utc"] for e in ev)   # de la más reciente hacia atrás
    # El acierto se lleva con contadores: al reiniciar se rehace igual desde la tabla.
    q = Predictor(bus, store, lambda: None, lambda: None, lambda: None, lambda: None)
    assert q.accuracy() == acc
    # Si una predicción se vuelve a evaluar, se corrige su acierto en vez de contarla dos veces.
    p._finish("BTC-londres-2026-10-21", 0.5)
    acc2 = p.accuracy()
    assert acc2["evaluated"] == 2 and acc2["hit_rate"] == 1.0 and acc2["brier"] < acc["brier"]
    assert Predictor(bus, store, lambda: None, lambda: None, lambda: None, lambda: None).accuracy() == acc2


def test_accuracy_no_relee_la_tabla(store):
    """accuracy() se llama en cada ronda: no puede volver a leer todas las predicciones finales (con su body)."""
    p, _, _ = make(store)
    p.run_round(NOW)
    calls = []

    class Spy:
        def __init__(self, db):
            self.db = db

        def execute(self, sql, *args):
            calls.append(sql)
            return self.db.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self.db, name)

    real = store.db
    store.db = Spy(real)
    try:
        p.accuracy()
        assert calls == []
    finally:
        store.db = real


def test_ventana_sin_precio_al_empezar_no_se_evalua(store):
    p, _, _ = crypto_world(store)
    p.run_round(NOW_CR)
    p.tick(datetime(2026, 10, 21, 7, 20, tzinfo=UTC))            # el bot no estaba a las 07:00
    out, hit = store.db.execute("SELECT outcome_pct, hit FROM predictions WHERE id='BTC-londres-2026-10-21'").fetchone()
    assert out is None and hit == -1 and p.accuracy()["evaluated"] == 0


def test_precios_y_flujo_viejos_no_se_usan(store):
    """market no trae hora: si no llega un mensaje nuevo en 2 min (WebSocket caído), sus precios no sirven ni de
    referencia ni para evaluar. El flujo de órdenes de hace más de 1 min tampoco cuenta."""
    p, _, box = crypto_world(store)
    # Flujo de órdenes de hace 5 min: fuera; de hace 10 s: dentro.
    of = box["of"]["coins"]
    box["of"] = {"ts": ms(NOW_CR) - 5 * 60_000, "coins": of}
    h = item(p.run_round(NOW_CR), "BTC-1h-2026-10-21T06:30")
    assert not any(d["factor"].startswith("Flujo") for d in h["drivers"])
    box["of"] = {"ts": ms(NOW_CR) - 10_000, "coins": of}
    h = item(p.run_round(NOW_CR), "BTC-1h-2026-10-21T06:30")
    assert any(d["factor"].startswith("Flujo") for d in h["drivers"])
    # Precios: el mismo mensaje de las 06:30, sin cambios, media hora después.
    p.tick(datetime(2026, 10, 21, 7, 0, 5, tzinfo=UTC))
    assert "BTC-londres-2026-10-21" not in p.refs and len(p.px["BTC"]) == 2
    box["market"] = {"prices": {"BTC": 101500.0}, "flow": {}}       # vuelve: mensaje nuevo
    p.tick(datetime(2026, 10, 21, 7, 1, tzinfo=UTC))
    assert p.refs["BTC-londres-2026-10-21"] == 101500.0
    # La próxima hora tampoco se evalúa con un precio parado desde hace más de 2 min.
    p.tick(datetime(2026, 10, 21, 7, 31, tzinfo=UTC))
    assert "BTC-1h-2026-10-21T06:30" in p.pending
    p.tick(datetime(2026, 10, 21, 7, 46, tzinfo=UTC))
    assert store.db.execute("SELECT hit FROM predictions WHERE id='BTC-1h-2026-10-21T06:30'").fetchone()[0] == -1
    # Si market trae hora, cuenta esa.
    assert p._fresh_prices({"ts": ms(NOW_CR) - 5 * 60_000, "prices": {"BTC": 1.0}}, ms(NOW_CR)) == {}
    assert p._fresh_prices({"ts": ms(NOW_CR) - 30_000, "prices": {"BTC": 1.0}}, ms(NOW_CR)) == {"BTC": 1.0}


def test_reinicio_en_el_mismo_tramo_no_duplica_la_proxima_hora(store):
    p, bus, box = crypto_world(store)
    p.run_round(NOW_CR)
    q = Predictor(bus, store, lambda: box["trend"], lambda: box["of"], lambda: box["market"], lambda: box["bt"])
    box["market"] = {"prices": {"BTC": 101200.0}, "flow": {}}
    q.run_round(NOW_CR + timedelta(minutes=2))                    # mismo tramo de 5 min: mismo id de próxima hora
    pid = "BTC-1h-2026-10-21T06:30"
    assert store.db.execute("SELECT COUNT(*) FROM predictions WHERE id=?", (pid,)).fetchone()[0] == 1
    assert q.pending[pid]["when_utc"] == ms(NOW_CR) and q.refs[pid] == 101000.0
    assert q.pending[pid]["ends_utc"] == ms(NOW_CR) + H


def test_nota_de_la_ventana_de_nueva_york(store):
    """La tasa base es la proporción de días en que subió; el backtest mide la ventana de las 09:30 desde las 09:00."""
    bt = {**BACKTEST, "strategies": BACKTEST["strategies"] + [
        strat("bolsa_a_cripto-tokio-btc-nuevayork-compra", "bolsa_a_cripto", 484, 0.52, 0.504, 0.3, "no_funciona",
              senal="tokio", ventana="nuevayork", direccion="compra", salida="4 h")]}
    p, _, _ = make(store, bt=bt)
    w = item(p.run_round(datetime(2026, 10, 21, 13, 0, tzinfo=UTC)), "BTC-nuevayork-2026-10-21")
    assert w["when_utc"] == ms(datetime(2026, 10, 21, 13, 30, tzinfo=UTC))
    assert w["note"] == ("Tasa base 50,4 %: BTC subió en las 4 h siguientes en el 50,4 % de 484 días del backtest "
                         "(medida desde las 09:00 de Nueva York, con velas de 1 h).")


# ---- Evaluación de bolsas

def test_evaluacion_bolsa_de_apertura_a_cierre_y_de_cierre_a_cierre(store):
    trend = {"markets": {"tokio": ls("2026-10-21", 1.1), "nuevayork": ls("2026-10-20", 0.5)}}
    p, _, box = make(store, trend)
    msg = p.run_round(NOW)
    assert item(msg, "frankfurt-2026-10-21")["prob_up"] > 0.5 and item(msg, "londres-2026-10-21")["prob_up"] > 0.5
    box["trend"] = {"markets": {
        "frankfurt": {"state": "abierta", "session_date": "2026-10-21", "spark": [24000.0, 24050.0]},
        "londres": {"state": "abierta", "session_date": "2026-10-21", "spark": [9000.0]}}}
    p.tick(datetime(2026, 10, 21, 7, 10, tzinfo=UTC))             # en plena sesión se guarda la apertura
    assert p.refs["frankfurt-2026-10-21"] == 24000.0
    box["trend"] = {"markets": {
        "frankfurt": {"state": "cerrada", "session_date": "2026-10-21", "spark": [24000.0, 24240.0],
                      **ls("2026-10-21", 0.8, 24240.0)},
        "londres": {"state": "cerrada", "session_date": "2026-10-21", "spark": [9000.0, 9100.0],
                    **ls("2026-10-21", -0.4, 9100.0)}}}
    p.tick(datetime(2026, 10, 21, 15, 45, tzinfo=UTC))            # aún dentro del margen de Yahoo
    assert "frankfurt-2026-10-21" in p.pending
    p.tick(datetime(2026, 10, 21, 15, 51, tzinfo=UTC))
    fr = store.db.execute("SELECT outcome_pct, hit FROM predictions WHERE id='frankfurt-2026-10-21'").fetchone()
    lo = store.db.execute("SELECT outcome_pct, hit FROM predictions WHERE id='londres-2026-10-21'").fetchone()
    assert fr == (pytest.approx(1.0), 1)                           # 24 000 → 24 240, de apertura a cierre
    assert lo == (pytest.approx(-0.4), 0)                          # cierre a cierre, aunque haya velas
    acc = p.accuracy()
    assert acc["by_kind"]["bolsa"] == {"evaluated": 2, "hit_rate": 0.5}


def test_apertura_a_cierre_sin_apertura_no_se_evalua(store):
    """Sin precio de apertura (bot apagado al empezar la sesión, gráfico de otro día) no se cambia a la medida de cierre
    a cierre: no se evalúa. La de cierre a cierre sí."""
    p, _, box = make(store)
    p.run_round(NOW)
    box["trend"] = {"markets": {"frankfurt": {"state": "cerrada", "session_date": "2026-10-20", "spark": [23900.0],
                                              **ls("2026-10-21", 0.8, 24240.0)},
                                "londres": {"state": "cerrada", **ls("2026-10-21", -0.4, 9100.0)}}}
    p.tick(datetime(2026, 10, 21, 15, 51, tzinfo=UTC))
    fr = store.db.execute("SELECT outcome_pct, hit FROM predictions WHERE id='frankfurt-2026-10-21'").fetchone()
    lo = store.db.execute("SELECT outcome_pct, hit FROM predictions WHERE id='londres-2026-10-21'").fetchone()
    assert fr == (None, -1) and lo == (pytest.approx(-0.4), 0)
    assert p.accuracy()["by_kind"]["bolsa"] == {"evaluated": 1, "hit_rate": 0.0}


def test_bolsa_sin_sesion_caduca(store):
    p, _, box = make(store)
    p.run_round(NOW)
    box["trend"] = {"markets": {"frankfurt": {"holiday": True, **ls("2026-10-20", 0.3)}}}
    p.tick(datetime(2026, 10, 22, 9, 0, tzinfo=UTC))
    assert "frankfurt-2026-10-21" in p.pending                    # aún puede llegar el dato
    p.tick(datetime(2026, 10, 24, 16, 0, tzinfo=UTC))
    assert store.db.execute("SELECT hit FROM predictions WHERE id='frankfurt-2026-10-21'").fetchone()[0] == -1


def test_el_acierto_cuenta_la_ultima_ronda_antes_del_evento(store):
    p, _, box = make(store, {"markets": {"nuevayork": {"futures": {"symbol": "ES=F", "change_pct": -3.0}}}})
    first = item(p.run_round(NOW - timedelta(hours=1)), "nuevayork-2026-10-21")
    box["trend"] = {"markets": {"nuevayork": {"futures": {"symbol": "ES=F", "change_pct": 1.0}}}}
    last = item(p.run_round(NOW), "nuevayork-2026-10-21")
    assert first["prob_up"] < 0.5 < last["prob_up"]
    box["trend"] = {"markets": {"nuevayork": {"session_date": "2026-10-21", "spark": [6000.0], **ls("2026-10-21", 0.3)}}}
    p.tick(datetime(2026, 10, 21, 20, 21, tzinfo=UTC))
    hits = [r[0] for r in store.db.execute("SELECT hit FROM predictions WHERE id='nuevayork-2026-10-21' ORDER BY ts")]
    assert hits == [0, 1]                                         # cada ronda marca su acierto
    assert p.accuracy()["by_kind"]["bolsa"] == {"evaluated": 1, "hit_rate": 1.0}


# ---- Registro, formato y robustez

def test_publica_el_formato_del_contrato(store):
    p, bus, _ = crypto_world(store)
    p.run_round(NOW_CR)
    msg = bus.state["predictions"]
    assert msg["type"] == "predictions" and msg["ts"] == ms(NOW_CR)
    assert set(msg["accuracy"]) >= {"evaluated", "hit_rate", "by_kind"}
    assert set(msg["accuracy"]["by_kind"]) == {"bolsa", "cripto"}
    json.dumps(msg)
    for it in msg["items"]:
        assert ITEM_KEYS <= set(it)
        assert 0.05 <= it["prob_up"] <= 0.95 and it["confidence"] in ("baja", "media", "alta")
        assert it["kind"] in ("bolsa", "cripto") and it["horizon"] in ("sesión", "1h", "4h")
        for d in it["drivers"]:
            assert set(d) == {"factor", "effect_pp", "source"} and isinstance(d["effect_pp"], float)
    assert p.view() is msg


def test_sin_duplicados_en_la_misma_ronda(store, monkeypatch):
    p, bus, _ = make(store)
    p.run_round(NOW)
    p.run_round(NOW)                                              # la misma ronda repetida no duplica filas
    per_id = store.db.execute("SELECT id, COUNT(*) FROM predictions GROUP BY id").fetchall()
    assert per_id and all(c == 1 for _, c in per_id)
    orig = p.predict_markets
    monkeypatch.setattr(p, "predict_markets", lambda ctx: orig(ctx) * 2)
    msg = p.run_round(NOW + timedelta(minutes=5))
    ids = [i["id"] for i in msg["items"]]
    assert len(ids) == len(set(ids))
    assert store.db.execute("SELECT COUNT(*) FROM predictions WHERE id='frankfurt-2026-10-21'").fetchone()[0] == 2


def test_al_reiniciar_recupera_lo_pendiente(store):
    p, bus, _ = crypto_world(store)
    p.run_round(NOW_CR)
    q = Predictor(bus, store, lambda: None, lambda: None, lambda: None, lambda: None)
    assert set(q.pending) == set(p.pending)
    assert q.pending["frankfurt-2026-10-21"]["measure"] == "apertura_a_cierre"
    assert q.refs == {"BTC-1h-2026-10-21T06:30": 101000.0}


def test_datos_que_fallan_no_tumban_la_ronda(store):
    def boom():
        raise RuntimeError("caído")
    p = Predictor(Bus(store), store, boom, boom, boom, boom)
    msg = p.run_round(NOW)
    fr = item(msg, "frankfurt-2026-10-21")
    assert fr["prob_up"] == 0.5 and fr["note"].startswith("Sin backtest todavía")
    assert item(msg, "BTC-1h-2026-10-21T06:45")["prob_up"] == 0.5
    p.tick(NOW + timedelta(minutes=1))


def test_poda_las_rondas_intermedias_antiguas(store):
    p, _, _ = make(store)
    old, when = ms(NOW) - 20 * 86400_000, ms(NOW) - 19 * 86400_000
    rows = [("x", old, "bolsa", "tokio", when, "sesión", 0.6, 0.1, 0.5, 1, "{}"),
            ("x", old + 300_000, "bolsa", "tokio", when, "sesión", 0.55, 0.1, 0.5, 1, "{}"),
            ("y", old, "bolsa", "seul", when, "sesión", 0.6, 0.1, None, None, "{}")]
    store.db.executemany("INSERT INTO predictions VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    p._prune(ms(NOW))
    left = store.db.execute("SELECT id, ts FROM predictions WHERE id IN ('x','y') ORDER BY id, ts").fetchall()
    assert left == [("x", old + 300_000), ("y", old)]


def test_backtest_indexado(store):
    bt = BacktestIndex(BACKTEST)
    assert bt.market_base("frankfurt") == (0.52, 2400, "apertura_a_cierre")
    assert bt.market_base("londres") == (0.535, 2400, "cierre_a_cierre")
    assert bt.market_base("madrid") is None
    assert bt.window_base("londres", "BTC") == (0.49, 2100, "bolsa_a_cripto-tokio-btc-londres-compra")
    s = bt.leadlag_strategy("nuevayork", "londres", 1, True)
    assert s["n"] == round(2300 * 0.546) and s["verdict"] == "dudosa" and s["hit_rate"] == 0.60
    assert bt.leadlag_strategy("madrid", "londres", 0, True) is None


def test_getter_de_resultados_lee_solo_si_cambia(mtmp, monkeypatch):
    path = mtmp / "resultados.json"
    monkeypatch.setattr(backtest, "results_path", lambda: path)
    get = predict.results_getter()
    assert get() is None
    path.write_text(json.dumps({"generated": 1}), encoding="utf-8")
    first = get()
    assert first == {"generated": 1} and get() is first
    path.write_text(json.dumps({"generated": 2}), encoding="utf-8")
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
    assert get() == {"generated": 2}
