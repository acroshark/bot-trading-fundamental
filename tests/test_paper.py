"""Paper trading con precios simulados: sin red, con reloj inyectable y sin deslizamiento salvo donde se indica."""
import asyncio
import json
import logging
import re
from datetime import datetime, timezone

import pytest

from bot import config, paper
from bot.bus import Bus
from bot.paper import PaperBroker, day_start_ms, market_of, next_midnight_ms
from bot.store import Store

UTC = timezone.utc
T0 = int(datetime(2026, 10, 23, 8, 0, tzinfo=UTC).timestamp() * 1000)   # viernes, 10:00 en Madrid
MIN = 60_000
FEE = 0.001   # 0,10 % por lado

EVENT_KEYS = {"type", "ts", "id", "kind", "agent", "story", "title", "text", "coins", "patch"}
RESULT_KEYS = {"coin", "side", "qty", "entry", "exit", "why", "pnl", "pnl_pct", "R", "fees", "opened", "closedAt",
               "riskUsd", "notional", "headline", "strategy", "window"}
TRADE_KEYS = {"id", "story", "strategy", "window", "market", "coin", "side", "qty", "entry", "exit", "sl_initial",
              "trail_pct", "opened", "closed", "reason", "pnl", "pnl_pct", "R", "fees", "headline", "news_score"}
POS_KEYS = {"story", "coin", "side", "qty", "entry", "sl", "trail_pct", "high_water", "opened", "riskUsd", "notional",
            "headline", "strategy", "window"}
AGENT = {"riskok": "risk", "riskko": "risk", "humanwait": "human", "humanok": "human", "humanko": "human",
         "scheduled": "exec", "order": "exec", "filled": "exec", "manage": "exec", "blocked": "exec", "closed": "exec"}
PATCH = {"riskok": "risk", "riskko": "risk", "humanwait": "human", "humanok": "human", "humanko": "human",
         "scheduled": "exec", "order": "exec", "filled": "exec", "manage": "exec", "blocked": "exec", "closed": "result"}


class Clock:
    def __init__(self, t: int):
        self.t = t

    def __call__(self) -> int:
        return self.t


class Env:
    """Bus, base de datos, precios y reloj de mentira alrededor de un PaperBroker."""

    def __init__(self, tmp_path):
        self.store = Store(tmp_path / "paper.db")
        self.bus = Bus(self.store)
        self.q = self.bus.subscribe()
        self.clock = Clock(T0)
        self.prices = {c: 100.0 for c in config.COINS}
        self.spread = {c: 0.0 for c in config.COINS}
        self.updated = None   # hora de cada precio (updated de market); None = el mensaje no la trae
        self.slip = 0.0
        self.seen: list[dict] = []
        self.broker = self.new()

    def market(self) -> dict:
        m = {"prices": self.prices, "spread": self.spread}
        if self.updated is not None:
            m["updated"] = self.updated
        return m

    def new(self) -> PaperBroker:
        return PaperBroker(self.bus, self.store, self.market, lambda: {"coins": {}}, clock=self.clock,
                           slip_bps=lambda: self.slip)

    def at(self, minutes: float = 0.0, **px) -> None:
        """Avanza el reloj a T0 + minutos, cambia precios y hace una ronda."""
        self.clock.t = T0 + int(minutes * MIN)
        self.prices.update(px)
        self.broker.step()

    def evs(self, kind: str | None = None, story: str | None = None) -> list[dict]:
        while not self.q.empty():
            m = json.loads(self.q.get_nowait())
            if m["type"] == "event":
                self.seen.append(m)
        return [e for e in self.seen if (kind is None or e["kind"] == kind) and (story is None or e["story"] == story)]

    def kinds(self, story: str) -> list[str]:
        return [e["kind"] for e in self.evs(story=story)]

    def result(self, story: str) -> dict:
        [c] = self.evs("closed", story)
        return c["patch"]["result"]


def ticket(coin="BTC", side="long", stop=2.0, trail=None, tp2=None, timing="now", winAt=None, risk=0.5, **kw) -> dict:
    """Ticket con el formato de patch.strat (2.6)."""
    return {"coin": coin, "side": side, "mode": "A favor de la noticia", "risk": risk, "stopPct": stop, "trailPct": trail,
            "timing": timing, "win": "Apertura Londres", "winAt": winAt, "entryType": "Mercado con límite de protección",
            "tp1R": 1.5, "tp2R": tp2, "valid": "60 min", "inval": "", "why": "", "llm": None, **kw}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in {"APPROVAL_MODE": "auto", "PAPER_EQUITY": 10000.0, "RISK_PER_TRADE_PCT": 0.5, "MAX_DAILY_LOSS_PCT": 3.0,
                 "MAX_POSITIONS": 4, "FEE_PCT": 0.10, "VETO_SECONDS": 30}.items():
        monkeypatch.setattr(config, k, v)
    e = Env(tmp_path)
    yield e
    e.store.close()


def fees(qty, entry, exit_):
    return qty * entry * FEE + qty * exit_ * FEE


# ---------------------------------------------------------------- utilidades

def test_bolsa_de_la_ventana_y_medianoche_de_madrid():
    assert market_of("Apertura Londres") == "londres"
    assert market_of("Apertura Nueva York") == "nuevayork"
    assert market_of("Hong Kong, 09:30") == "hongkong"
    assert market_of("Reacción inmediata") is None
    assert day_start_ms(T0) == int(datetime(2026, 10, 22, 22, 0, tzinfo=UTC).timestamp() * 1000)   # CEST
    assert next_midnight_ms(T0) == int(datetime(2026, 10, 23, 22, 0, tzinfo=UTC).timestamp() * 1000)
    dst = int(datetime(2026, 10, 25, 10, 0, tzinfo=UTC).timestamp() * 1000)   # el 25-10 dura 25 h
    assert next_midnight_ms(dst) == int(datetime(2026, 10, 25, 23, 0, tzinfo=UTC).timestamp() * 1000)


# ---------------------------------------------------------------- tamaño y ejecución

def test_tamano_por_riesgo_con_costes_y_tope_del_40_por_ciento(env):
    b = env.broker
    assert b.submit(ticket(stop=2), "n1")   # 50 $ en el stop contando comisiones y deslizamiento previsto
    p = b.positions["n1"]
    exit_ = 98 * (1 - 2 / 10000)            # stop a 98 y salida con 2 pb (sin diferencial)
    q = 50 / (100 - exit_ + 100 * FEE + exit_ * FEE)
    assert p["qty"] == pytest.approx(q) and p["notional"] == pytest.approx(q * 100) and q < 25   # sin costes serían 25
    assert p["riskUsd"] == pytest.approx(p["notional"] * 0.02)   # R se sigue midiendo hasta el stop (como el backtest)
    assert "(50,00 con costes)" in env.evs("filled", "n1")[0]["text"]
    assert b.submit(ticket(coin="ETH", stop=1), "n2")   # unos 4.100 → tope de 4.000 (40 %)
    p = b.positions["n2"]
    assert p["notional"] == pytest.approx(4000, rel=1e-3) and p["riskUsd"] == pytest.approx(40, rel=1e-3)
    assert env.kinds("n1") == ["riskok", "humanok", "order", "filled"]


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("spread", [0.0, 5.0])
def test_perdida_en_el_stop_con_costes_no_pasa_del_riesgo(env, side, spread):
    env.spread["BTC"] = spread
    env.slip = paper.COST_SLIP_BPS   # el peor deslizamiento simulado, al entrar y al salir
    assert env.broker.submit(ticket(side=side, stop=2), "n1")
    p = env.broker.positions["n1"]
    assert env.evs("filled", "n1")[0]["patch"]["exec"]["stopLossUsd"] == pytest.approx(50, abs=0.01)
    env.at(1, BTC=p["sl"])           # toca el stop justo
    r = env.result("n1")
    assert r["why"] == "Stop de pérdida" and r["pnl"] >= -50.0001 and r["pnl"] == pytest.approx(-50, abs=0.01)
    assert r["fees"] == pytest.approx(r["qty"] * (r["entry"] + r["exit"]) * FEE, abs=1e-3)


def test_precio_de_entrada_con_medio_diferencial_y_deslizamiento(env):
    env.spread["BTC"] = env.spread["ETH"] = 2.0
    env.slip = 1.0   # 1 pb (medio diferencial) + 1 pb = 2 pb en contra
    env.broker.submit(ticket(), "n1")
    env.broker.submit(ticket(coin="ETH", side="short"), "n2")
    assert env.broker.positions["n1"]["entry"] == pytest.approx(100.02)
    assert env.broker.positions["n2"]["entry"] == pytest.approx(99.98)
    f = env.evs("filled", "n1")[0]["patch"]["exec"]
    assert f["slip"] == pytest.approx(2.0) and f["px"] == pytest.approx(100.02) and f["notional"] > 0


# ---------------------------------------------------------------- gestión: largos

def test_largo_stop_dinamico_con_break_even(env):
    env.broker.submit(ticket(stop=2, trail=1), "n1", "Titular", "seguir-bolsa-anterior", "Apertura Londres")
    p = env.broker.positions["n1"]
    q = p["qty"]
    env.at(1, BTC=101)
    assert p["sl"] == pytest.approx(99.99) and not p["be"]   # 1 % bajo el máximo
    env.at(2, BTC=102)                                        # +1 R: break-even y el dinámico sigue por encima
    assert p["be"] and p["sl"] == pytest.approx(100.98)
    env.at(3, BTC=103)
    assert p["sl"] == pytest.approx(101.97) and p["high_water"] == 103
    env.at(4, BTC=101.9)
    assert "n1" not in env.broker.positions
    r = env.result("n1")
    assert r["why"] == "Stop dinámico" and r["exit"] == pytest.approx(101.9)
    assert r["fees"] == pytest.approx(fees(q, 100, 101.9), abs=1e-4)   # va redondeado
    assert r["pnl"] == pytest.approx(1.9 * q - fees(q, 100, 101.9), abs=1e-3)
    assert r["R"] == pytest.approx(r["pnl"] / (q * 2), abs=1e-3)   # riesgo hasta el stop: 2 % del nominal
    assert env.evs("closed", "n1")[0]["tone"] == "good"
    manage = env.evs("manage", "n1")
    assert 2 <= len(manage) <= 4 and any("break-even" in m["title"] for m in manage)


def _qty(env, story="n1") -> float:
    return env.broker.positions[story]["qty"]


def test_largo_break_even(env):
    env.broker.submit(ticket(stop=2), "n1")
    q = _qty(env)
    env.at(1, BTC=102)
    assert env.broker.positions["n1"]["sl"] == pytest.approx(100)
    env.at(2, BTC=100)
    r = env.result("n1")
    assert r["why"] == "Stop en break-even"
    assert r["pnl"] == pytest.approx(-fees(q, 100, 100), abs=1e-4)   # solo pierde las comisiones
    assert env.evs("closed", "n1")[0]["tone"] == "bad"


def test_largo_stop_de_perdida(env):
    env.broker.submit(ticket(stop=2), "n1")
    q = _qty(env)
    env.at(1, BTC=99)
    assert "n1" in env.broker.positions
    env.at(2, BTC=97.9)   # salta por debajo del stop (98): sale al precio visto
    r = env.result("n1")
    assert r["why"] == "Stop de pérdida" and r["exit"] == pytest.approx(97.9)
    assert r["pnl"] == pytest.approx(-2.1 * q - fees(q, 100, 97.9), abs=1e-3)
    assert r["R"] < -1


def test_largo_objetivo(env):
    env.broker.submit(ticket(stop=2, tp2=3), "n1")
    q = _qty(env)
    assert env.broker.positions["n1"]["tp2"] == pytest.approx(106)
    env.at(5, BTC=106.5)   # orden límite: sale al precio del objetivo
    r = env.result("n1")
    assert r["why"] == "Objetivo (3,0 R)" and r["exit"] == pytest.approx(106)
    assert r["pnl"] == pytest.approx(6 * q - fees(q, 100, 106), abs=1e-3)
    assert r["R"] == pytest.approx(r["pnl"] / (q * 2), abs=1e-3)


def test_largo_cierre_por_tiempo(env):
    env.broker.submit(ticket(stop=2), "n1")
    q = _qty(env)
    env.at(239, BTC=100.5)
    assert "n1" in env.broker.positions
    env.at(240, BTC=100.5)
    r = env.result("n1")
    assert r["why"] == "Cierre por tiempo (4 h)"
    assert r["pnl"] == pytest.approx(0.5 * q - fees(q, 100, 100.5), abs=1e-3)


# ---------------------------------------------------------------- gestión: cortos

def test_corto_stop_dinamico(env):
    env.broker.submit(ticket(side="short", stop=2, trail=1), "n1")
    p = env.broker.positions["n1"]
    q = p["qty"]
    assert p["sl"] == pytest.approx(102)
    env.at(1, BTC=99)
    assert p["sl"] == pytest.approx(99.99) and p["high_water"] == 99   # en un corto el extremo favorable es el mínimo
    env.at(2, BTC=98)
    assert p["be"] and p["sl"] == pytest.approx(98.98)
    env.at(3, BTC=99)
    r = env.result("n1")
    assert r["why"] == "Stop dinámico" and r["side"] == "short"
    assert r["pnl"] == pytest.approx(1 * q - fees(q, 100, 99), abs=1e-3) and r["pnl"] > 0


def test_trailing_mas_corto_que_el_stop_no_manda_desde_la_entrada_en_largo(env):
    env.broker.submit(ticket(stop=2, trail=0.5), "n1")
    p = env.broker.positions["n1"]
    env.at(1, BTC=100)                     # sin avanzar a favor manda el stop inicial (con el que se calculó el tamaño)
    assert p["sl"] == pytest.approx(98) and p["sl_kind"] == "inicial"
    env.at(2, BTC=99.4)
    assert "n1" in env.broker.positions
    env.at(3, BTC=100.75)                  # +0,75 %: la distancia baja de 2 % a 1,25 %
    assert p["sl"] == pytest.approx(100.75 * (1 - 0.0125)) and p["sl_kind"] == "dinamico" and not p["be"]
    assert env.evs("manage", "n1")[-1]["text"].startswith("1,25 % desde el máximo de 100,75")
    env.at(4, BTC=101.5)                   # +1,5 %: ya a la distancia del trailing (0,5 %), antes del break-even
    assert p["sl"] == pytest.approx(101.5 * 0.995) and not p["be"]
    env.at(5, BTC=100.9)
    r = env.result("n1")
    assert r["why"] == "Stop dinámico" and r["exit"] == pytest.approx(100.9) and r["pnl"] > 0


def test_trailing_mas_corto_que_el_stop_no_manda_desde_la_entrada_en_corto(env):
    env.broker.submit(ticket(side="short", stop=2, trail=0.5), "n1")
    p = env.broker.positions["n1"]
    q = p["qty"]
    env.at(1, BTC=100)
    assert p["sl"] == pytest.approx(102) and p["sl_kind"] == "inicial"
    env.at(2, BTC=100.6)
    assert "n1" in env.broker.positions
    env.at(3, BTC=99.25)
    assert p["sl"] == pytest.approx(99.25 * 1.0125) and p["sl_kind"] == "dinamico"
    env.at(4, BTC=98.5)
    assert p["sl"] == pytest.approx(98.5 * 1.005)
    env.at(5, BTC=99.0)
    r = env.result("n1")
    assert r["why"] == "Stop dinámico" and r["pnl"] == pytest.approx(1 * q - fees(q, 100, 99), abs=1e-3)


def test_corto_break_even(env):
    env.broker.submit(ticket(side="short", stop=2), "n1")
    q = _qty(env)
    env.at(1, BTC=97.5)
    assert env.broker.positions["n1"]["sl"] == pytest.approx(100)
    env.at(2, BTC=100.1)
    r = env.result("n1")
    assert r["why"] == "Stop en break-even"
    assert r["pnl"] == pytest.approx(-0.1 * q - fees(q, 100, 100.1), abs=1e-3)


def test_corto_stop_de_perdida(env):
    env.broker.submit(ticket(side="short", stop=2), "n1")
    q = _qty(env)
    env.at(1, BTC=102.5)
    r = env.result("n1")
    assert r["why"] == "Stop de pérdida" and r["exit"] == pytest.approx(102.5)
    assert r["pnl"] == pytest.approx(-2.5 * q - fees(q, 100, 102.5), abs=1e-3) and r["R"] < -1


def test_corto_objetivo(env):
    env.broker.submit(ticket(side="short", stop=2, tp2=3), "n1")
    q = _qty(env)
    env.at(10, BTC=93.0)
    r = env.result("n1")
    assert r["why"] == "Objetivo (3,0 R)" and r["exit"] == pytest.approx(94)
    assert r["pnl"] == pytest.approx(6 * q - fees(q, 100, 94), abs=1e-3) and r["pnl"] > 0


def test_corto_cierre_por_tiempo_con_duracion_del_ticket(env):
    env.broker.submit(ticket(side="short", stop=2, holdMin=60), "n1")
    q = _qty(env)
    env.at(61, BTC=100.5)
    r = env.result("n1")
    assert r["why"] == "Cierre por tiempo (60 min)"
    assert r["pnl"] == pytest.approx(-0.5 * q - fees(q, 100, 100.5), abs=1e-3)


def test_cierre_manual_con_deslizamiento_en_contra(env):
    env.broker.submit(ticket(stop=2), "n1")
    env.slip = 1.0
    env.prices["BTC"] = 101.0
    assert env.broker.close("n1") and not env.broker.close("n1")
    r = env.result("n1")
    assert r["why"] == "Cierre manual" and r["exit"] == pytest.approx(101 * (1 - 1 / 10000))


# ---------------------------------------------------------------- salidas por ticket (beR, trailStartR, tp1Frac)

def test_reglas_de_salida_compatibles_hacia_atras():
    old = paper.exit_rules({"trailPct": 1.0, "tp1R": 1.5, "tp2R": None})
    assert old == {"trail": 1.0, "be_r": 1.0, "be_pct": 0.0, "trail_start_r": None, "tp1R": 1.5, "tp1_frac": 0.0,
                   "tp2R": None}                                     # sin campos nuevos: como siempre
    assert paper.cost_rt_pct() == pytest.approx(0.24) and paper.be_offset_pct() == pytest.approx(0.26)
    new = paper.exit_rules({"beR": None, "trailStartR": 1.5, "tp1R": 1.0, "tp1Frac": 0.5, "tp2R": 2})
    assert new["be_r"] is None and new["be_pct"] == pytest.approx(0.26) and new["trail_start_r"] == 1.5
    assert new["tp1_frac"] == 0.5 and new["tp2R"] == 2
    assert paper.exit_rules({"beR": 2})["be_r"] == 2 and paper.exit_rules({"tp1Frac": 0.5})["tp1_frac"] == 0.0


@pytest.mark.parametrize("side", ["long", "short"])
def test_sin_break_even(env, side):
    d = 1 if side == "long" else -1
    env.broker.submit(ticket(side=side, stop=2, beR=None), "n1")
    p = env.broker.positions["n1"]
    env.at(1, BTC=100 + d * 3)             # +1,5 R: el stop no se mueve
    assert p["sl"] == pytest.approx(100 - d * 2) and not p["be"] and not env.evs("manage", "n1")
    env.at(2, BTC=100 - d * 2.1)
    r = env.result("n1")
    assert r["why"] == "Stop de pérdida" and r["exit"] == pytest.approx(100 - d * 2.1) and r["R"] < -1


def test_largo_break_even_a_1_5_r_cubre_costes(env):
    env.broker.submit(ticket(stop=2, beR=1.5), "n1")
    p = env.broker.positions["n1"]
    q = p["qty"]
    env.at(1, BTC=102)                     # +1 R: aún no
    assert p["sl"] == pytest.approx(98) and not p["be"]
    env.at(2, BTC=103)                     # +1,5 R: entrada + 0,24 % de costes + 2 pb
    assert p["be"] and p["sl"] == pytest.approx(100.26) and p["sl_kind"] == "be"
    assert "cubre las comisiones" in env.evs("manage", "n1")[-1]["text"]
    env.at(3, BTC=100.25)
    r = env.result("n1")
    assert r["why"] == "Stop en break-even" and r["exit"] == pytest.approx(100.25)
    assert r["pnl"] == pytest.approx(0.25 * q - fees(q, 100, 100.25), abs=1e-4) and r["pnl"] > 0
    assert env.evs("closed", "n1")[0]["tone"] == "good"


def test_corto_break_even_a_2_r_cubre_costes(env):
    env.broker.submit(ticket(side="short", stop=2, beR=2), "n1")
    p = env.broker.positions["n1"]
    q = p["qty"]
    env.at(1, BTC=97)                      # +1,5 R
    assert p["sl"] == pytest.approx(102) and not p["be"]
    env.at(2, BTC=96)                      # +2 R
    assert p["be"] and p["sl"] == pytest.approx(99.74)
    env.at(3, BTC=99.74)
    r = env.result("n1")
    assert r["why"] == "Stop en break-even"
    assert r["pnl"] == pytest.approx(0.26 * q - fees(q, 100, 99.74), abs=1e-4) and r["pnl"] > 0


@pytest.mark.parametrize("side", ["long", "short"])
def test_stop_dinamico_solo_desde_trail_start_r(env, side):
    d = 1 if side == "long" else -1
    px = lambda x: 100 + d * x
    env.broker.submit(ticket(side=side, stop=2, trail=1, beR=None, trailStartR=1.5), "n1")
    p = env.broker.positions["n1"]
    q = p["qty"]
    env.at(1, BTC=px(1))                   # +0,5 R: sin trailStartR el stop ya subiría a 99,99
    assert p["sl"] == pytest.approx(px(-2)) and p["sl_kind"] == "inicial"
    env.at(2, BTC=px(2.5))                 # +1,25 R: todavía no
    assert p["sl"] == pytest.approx(px(-2))
    env.at(3, BTC=px(3))                   # +1,5 R: a 1 % del extremo
    assert p["sl"] == pytest.approx(px(3) * (1 - d * 0.01)) and p["sl_kind"] == "dinamico"
    env.at(4, BTC=px(1.9))
    r = env.result("n1")
    assert r["why"] == "Stop dinámico" and r["exit"] == pytest.approx(px(1.9))
    assert r["pnl"] == pytest.approx(1.9 * q - fees(q, 100, px(1.9)), abs=1e-3)


def test_largo_salida_parcial_y_objetivo(env):
    env.broker.submit(ticket(stop=2, tp1R=1.0, tp1Frac=0.5, tp2=3, beR=None), "n1")
    b, p = env.broker, env.broker.positions["n1"]
    q, h = p["qty"], p["qty"] / 2
    assert p["tp1"] == pytest.approx(102) and p["tp2"] == pytest.approx(106)
    env.at(1, BTC=102.5)                   # parcial: la mitad al precio de tp1 (orden límite)
    part = h * 2 - h * 102 * FEE
    assert p["qty"] == pytest.approx(h) and p["tp1_done"] and p["realized"] == pytest.approx(part)
    m = env.evs("manage", "n1")[-1]
    assert "salida parcial" in m["title"] and m["patch"]["exec"]["partial"]["qty"] == pytest.approx(h)
    assert b.equity() == pytest.approx(10000 + h * 2.5 - q * 100 * FEE + part)
    assert b._committed() == pytest.approx(h * 100)   # la exposición baja con la parcial
    pos = b.portfolio()["positions"][0]
    assert pos["tp1_done"] and pos["qty"] == pytest.approx(h) and pos["realized"] == pytest.approx(part, abs=0.01)
    env.at(2, BTC=106.5)                   # el resto, en el objetivo
    r = env.result("n1")
    total_fees = q * 100 * FEE + h * 102 * FEE + h * 106 * FEE
    assert r["why"] == "Objetivo (3,0 R) tras salida parcial"
    assert r["fees"] == pytest.approx(total_fees, abs=1e-4)
    assert r["pnl"] == pytest.approx(h * 2 + h * 6 - total_fees, abs=1e-3)
    assert r["R"] == pytest.approx(r["pnl"] / (q * 2), abs=1e-3)    # sobre el riesgo inicial
    assert r["qty"] == pytest.approx(q) and r["exit"] == pytest.approx(104)   # precio medio de las salidas
    assert r["pnl"] == pytest.approx((r["exit"] - r["entry"]) * r["qty"] - r["fees"], abs=1e-3)
    assert r["pnl_pct"] == pytest.approx(r["pnl"] / (q * 100) * 100, abs=1e-3)
    t = b.results()["trades"][0]
    assert t["partials"][0]["px"] == pytest.approx(102) and t["exit_last"] == pytest.approx(106)
    assert b.results()["equity"] == pytest.approx(10000 + r["pnl"], abs=0.01)


def test_corto_salida_parcial_y_stop_del_resto(env):
    env.broker.submit(ticket(side="short", stop=2, tp1R=1.0, tp1Frac=0.5, beR=None), "n1")
    q = _qty(env)
    h = q / 2
    env.at(1, BTC=97.5)                    # parcial a 98
    assert env.broker.positions["n1"]["qty"] == pytest.approx(h)
    b2 = env.new()                         # la parcial sobrevive al reinicio
    assert b2.positions["n1"]["tp1_done"] and b2.positions["n1"]["qty"] == pytest.approx(h)
    env.broker = b2
    env.at(2, BTC=102.4)                   # el resto sale en el stop inicial (102) visto a 102,4
    r = env.result("n1")
    total_fees = q * 100 * FEE + h * 98 * FEE + h * 102.4 * FEE
    assert r["why"] == "Stop de pérdida tras salida parcial" and r["side"] == "short"
    assert r["fees"] == pytest.approx(total_fees, abs=1e-4)
    assert r["pnl"] == pytest.approx(h * 2 - h * 2.4 - total_fees, abs=1e-3)
    assert r["R"] == pytest.approx(r["pnl"] / (q * 2), abs=1e-3) and r["exit"] == pytest.approx(100.2)
    assert r["pnl"] == pytest.approx(-(r["exit"] - r["entry"]) * r["qty"] - r["fees"], abs=1e-3)


@pytest.mark.parametrize("side", ["long", "short"])
def test_parcial_del_100_por_cien_es_el_objetivo(env, side):
    d = 1 if side == "long" else -1
    env.broker.submit(ticket(side=side, stop=2, tp1R=1.0, tp1Frac=1.0), "n1")
    q = _qty(env)
    env.at(1, BTC=100 + d * 2.3)
    r = env.result("n1")
    assert r["why"] == "Objetivo (1,0 R)" and r["exit"] == pytest.approx(100 + d * 2)
    assert r["pnl"] == pytest.approx(2 * q - fees(q, 100, 100 + d * 2), abs=1e-3) and r["pnl"] > 0


# ---------------------------------------------------------------- límites de riesgo

def _why(env, story):
    return env.evs("riskko", story)[0]["patch"]["risk"]["why"]


def test_riesgo_por_operacion_y_diferencial(env):
    assert not env.broker.submit(ticket(risk=0.8), "n1")
    assert _why(env, "n1") == "Riesgo por operación 0,80 % > 0,50 % permitido"   # el texto dice lo que ha pasado
    env.spread["BTC"] = 6.0
    assert not env.broker.submit(ticket(), "n2")
    assert _why(env, "n2") == "Diferencial de BTC: 6,0 pb > 5 pb"
    risk = env.evs("riskko", "n2")[0]["patch"]["risk"]
    assert risk["ok"] is False and len(risk["checks"]) == 8 and all({"ok", "t"} <= set(c) for c in risk["checks"])
    assert risk["checks"][1]["t"] == ("Riesgo por operación 0,50 % ≤ 0,50 % del capital (en el stop, con comisiones "
                                      "y deslizamiento)")   # los que pasan, con «≤»


def test_una_sola_operacion_por_moneda(env):
    assert env.broker.submit(ticket(), "n1")
    assert not env.broker.submit(ticket(side="short"), "n2")   # ni a la contra ni doblando riesgo en BTC
    assert "una sola por moneda" in env.evs("riskko", "n2")[0]["patch"]["risk"]["why"]
    assert env.broker.submit(ticket(coin="ETH"), "n3")


def test_ventana_no_se_veta_por_el_diferencial_del_momento_de_enviarla(env):
    env.spread["BTC"] = 6.0
    assert env.broker.submit(ticket(timing="window", winAt=T0 + 120 * MIN), "n1")
    checks = env.evs("riskok", "n1")[0]["patch"]["risk"]["checks"]
    assert checks[5] == {"ok": True, "t": "Diferencial de BTC: se comprueba al ejecutar (ahora 6,0 pb)"}
    assert "n1" in env.broker.scheduled


def test_ticket_no_valido_y_noticia_repetida(env):
    assert not env.broker.submit(ticket(coin="DOGE"), "n1")
    assert "Moneda no operable" in _why(env, "n1")
    assert not env.broker.submit(ticket(timing="window"), "n2")
    assert "winAt" in _why(env, "n2")
    assert env.broker.submit(ticket(), "n3")
    assert not env.broker.submit(ticket(coin="ETH"), "n3")
    assert _why(env, "n3") == "Ya hay una operación en curso para esta noticia"


def test_maximo_de_posiciones_cuenta_las_comprometidas(env, monkeypatch):
    monkeypatch.setattr(config, "MAX_POSITIONS", 2)
    assert env.broker.submit(ticket(), "n1")
    assert env.broker.submit(ticket(coin="ETH", timing="window", winAt=T0 + 60 * MIN), "n2")   # programada
    assert not env.broker.submit(ticket(coin="SOL"), "n3")
    assert _why(env, "n3") == "Posiciones abiertas o en curso 2 de 2: no caben más"


def test_exposicion_maxima(env):
    assert env.broker.submit(ticket(stop=1), "n1")              # 40 % cada una
    assert env.broker.submit(ticket(coin="ETH", stop=1), "n2")
    assert not env.broker.submit(ticket(coin="SOL", stop=1), "n3")
    assert _why(env, "n3") == "Exposición total 120 % > 100 % del capital"


def test_pausa_macro_solo_para_entradas_inmediatas(env):
    env.bus.publish({"type": "calendar", "events": [{"t": T0 + 5 * MIN, "name": "Consumer Price Index", "src": "BLS",
                                                     "impact": "alto"}], "updated": T0})
    assert not env.broker.submit(ticket(), "n1")
    assert _why(env, "n1") == "Dato macro de alto impacto en 5 min: Consumer Price Index"
    assert env.broker.submit(ticket(timing="window", winAt=T0 + 60 * MIN), "n2")
    assert env.kinds("n2") == ["riskok", "humanok", "scheduled"]


def test_perdida_maxima_del_dia_bloquea_hasta_la_medianoche(env):
    env.broker.submit(ticket(stop=1), "n1")   # nominal 4.000
    env.at(1, BTC=90)                         # hueco del 10 %: pierde 400 $ y comisiones (4 % del capital)
    assert env.result("n1")["pnl"] < -400
    assert env.broker.halt_until == next_midnight_ms(env.clock.t)
    assert not env.broker.submit(ticket(coin="ETH"), "n2")
    assert _why(env, "n2").startswith("Operativa detenida hasta la medianoche de Madrid")
    lim = env.broker.portfolio()["limits"]
    assert lim["day_loss_pct"] > 3
    # La fase 2 y el motor de aperturas leen limits.halted para no pagar ni enviar tickets que se vetarán seguro.
    assert lim["halted"] is True and lim["halt_until"] == env.broker.halt_until
    assert env.broker.portfolio()["halted"] is False       # no es la parada manual
    assert env.new().halt_until == env.broker.halt_until   # se recuerda tras reiniciar
    env.clock.t = env.broker.halt_until + 1000
    env.broker.step()
    assert env.broker.halt_until == 0
    assert env.broker.portfolio()["limits"]["halted"] is False   # la ronda lo quita sin esperar a un submit
    assert env.broker.submit(ticket(coin="ETH"), "n3")
    assert env.broker.portfolio()["day_pnl"] == pytest.approx(-env.broker.positions["n3"]["fee_open"], abs=0.01)


def test_perdida_del_dia_cuenta_desde_la_medianoche_con_posiciones_que_pasan_la_noche(env, monkeypatch):
    monkeypatch.setattr(config, "RISK_PER_TRADE_PCT", 5.0)
    env.clock.t = T0 + 780 * MIN                                     # 23:00 en Madrid
    assert env.broker.submit(ticket(risk=4.5, stop=10, holdMin=600), "n1")   # nominal 4.000 (tope): 40 BTC a 100
    env.at(839, BTC=95)                                              # 23:59: -204 $ latentes (2,04 %), sin parar
    assert env.broker.halt_until == 0
    env.at(841, BTC=95)                                              # 00:01: foto del capital a precio de mercado
    assert env.broker.day_base == {"date": "2026-10-24", "equity": pytest.approx(10000 - 204)}
    assert env.broker.portfolio()["day_pnl"] == pytest.approx(0, abs=0.01)
    env.at(842, BTC=91.5)                                            # hoy pierde 140 $ (1,43 %); desde la entrada, 3,44 %
    assert env.broker.halt_until == 0 and "n1" in env.broker.positions
    pf = env.broker.portfolio()
    assert pf["day_pnl"] == pytest.approx(-140, abs=0.01) and pf["limits"]["day_loss_pct"] < 3
    assert env.new().portfolio()["day_pnl"] == pytest.approx(-140, abs=0.01)   # la foto sobrevive al reinicio


# ---------------------------------------------------------------- precios sin actualizar

def test_precio_sin_actualizar_no_entra_ni_mueve_stops_ni_cierra(env):
    env.updated = {c: T0 for c in config.COINS}
    assert env.broker.submit(ticket(), "n1")
    assert env.broker.submit(ticket(coin="ETH", holdMin=1), "n2")
    env.at(2, BTC=97)                     # el WebSocket se cayó: market se reenvía con precios de hace 2 min
    assert set(env.broker.positions) == {"n1", "n2"}   # ni salta el stop (98) ni el cierre por tiempo (1 min)
    assert env.bus.state["agent:exec"]["patch"]["now"].startswith("Sin precio actualizado de BTC, ETH")
    assert not env.broker.close("n1")
    assert env.bus.state["agent:exec"]["patch"]["now"] == ("No se puede cerrar BTC ahora: "
                                                          "Precio de BTC sin actualizar desde hace 2 min")
    assert not env.broker.submit(ticket(coin="SOL"), "n3")
    assert _why(env, "n3") == "Precio de SOL sin actualizar desde hace 2 min"
    assert env.broker.submit(ticket(coin="XRP", timing="window", winAt=T0 + 3 * MIN), "n4")   # programada: espera

    env.updated.update(BTC=T0 + 2 * MIN, ETH=T0 + 2 * MIN)   # vuelven los precios
    env.at(2)
    assert env.result("n1")["why"] == "Stop de pérdida" and env.result("n1")["exit"] == pytest.approx(97)
    assert env.result("n2")["why"] == "Cierre por tiempo (1 min)"
    env.at(10)
    env.at(33)
    b = env.evs("blocked", "n4")[0]
    assert "lo último que la frenó: Precio de XRP sin actualizar desde hace 10 min" in b["text"]


# ---------------------------------------------------------------- aprobación

def test_modo_veto_se_aprueba_solo(env):
    env.broker.set_mode("veto")
    env.broker.submit(ticket(), "n1")
    w = env.evs("humanwait", "n1")[0]
    assert w["patch"]["human"] == {"mode": "veto", "deadline": T0 + 30_000}
    env.at(0.25)
    assert "n1" in env.broker.pending and "n1" not in env.broker.positions
    assert env.broker.portfolio()["pending"][0]["story"] == "n1"
    env.at(0.5)
    ok = env.evs("humanok", "n1")[0]["patch"]["human"]
    assert ok["by"] == "Sin veto en 30 s: aprobada" and ok["mode"] == "veto" and ok["at"] == T0 + 30_000
    assert env.kinds("n1") == ["riskok", "humanwait", "humanok", "order", "filled"]


def test_modo_veto_vetada(env):
    env.broker.set_mode("veto")
    env.broker.submit(ticket(), "n1")
    assert env.broker.decide("n1", False, "Rechazada por ti")
    assert not env.broker.decide("n1", True)
    ko = env.evs("humanko", "n1")[0]["patch"]["human"]
    assert ko == {"mode": "veto", "by": "Rechazada por ti", "at": T0, "no": True}
    env.at(1)
    assert not env.broker.positions and env.kinds("n1") == ["riskok", "humanwait", "humanko"]


def test_modo_manual_aprobada_y_caducada(env):
    env.broker.set_mode("manual")
    env.broker.submit(ticket(), "n1")
    assert env.evs("humanwait", "n1")[0]["patch"]["human"]["deadline"] == T0 + 10 * MIN
    env.at(1)
    assert env.broker.decide("n1", True)
    assert env.evs("humanok", "n1")[0]["patch"]["human"]["by"] == "Aprobada por ti" and "n1" in env.broker.positions
    env.broker.submit(ticket(coin="ETH"), "n2")
    env.at(10.9)
    assert "n2" in env.broker.pending
    env.at(11)
    assert env.evs("humanko", "n2")[0]["patch"]["human"]["by"] == "Caducada sin respuesta"
    assert "n2" not in env.broker.positions


def test_modo_automatico(env):
    assert not env.broker.set_mode("yolo") and env.broker.mode == "auto"
    env.broker.submit(ticket(), "n1")
    h = env.evs("humanok", "n1")[0]["patch"]["human"]
    assert h == {"mode": "auto", "by": "Automático (paper)", "at": T0}


def test_aprobada_tarde_se_bloquea_si_cambian_las_condiciones(env):
    env.broker.set_mode("manual")
    env.broker.submit(ticket(), "n1")
    env.spread["BTC"] = 8.0
    env.broker.decide("n1", True)
    b = env.evs("blocked", "n1")[0]
    assert b["patch"]["exec"]["blocked"] is True and "Diferencial de BTC 8,0 pb" in b["text"]
    assert not env.broker.positions


# ---------------------------------------------------------------- entradas programadas

def test_ventana_rompe_el_rango_de_los_5_primeros_minutos(env):
    at = T0 + 10 * MIN
    env.broker.submit(ticket(timing="window", winAt=at), "n1", "Titular", "encadenamiento", "Apertura Londres")
    s = env.evs("scheduled", "n1")[0]
    assert s["patch"]["exec"]["scheduled"] == at
    assert env.broker.portfolio()["scheduled"][0]["at"] == at
    env.at(4, BTC=105)          # antes del rango: no cuenta
    env.at(5, BTC=100)
    env.at(6, BTC=101)
    env.at(8, BTC=99.5)
    env.at(10, BTC=100.8)       # fin del rango (99,5 - 101): aún no rompe
    env.at(11, BTC=101.0)       # igual al máximo: no rompe
    assert "n1" not in env.broker.positions
    env.at(12, BTC=101.2)
    p = env.broker.positions["n1"]
    assert p["entry"] == pytest.approx(101.2) and p["market"] == "londres"
    assert env.kinds("n1") == ["riskok", "humanok", "scheduled", "order", "filled"]
    assert env.evs("filled", "n1")[0]["patch"]["exec"]["range"] == {"hi": 101, "lo": 99.5}
    assert not env.broker.scheduled


def test_ventana_corta_rompe_por_abajo(env):
    at = T0 + 10 * MIN
    env.broker.submit(ticket(side="short", timing="window", winAt=at), "n1")
    for m, px in ((5, 100), (7, 100.4), (9, 99.8)):
        env.at(m, BTC=px)
    env.at(10, BTC=100.6)       # rompe por arriba: un corto no entra
    assert "n1" not in env.broker.positions
    env.at(13, BTC=99.7)
    assert env.broker.positions["n1"]["entry"] == pytest.approx(99.7)


def test_ventana_sin_ruptura_caduca_a_los_30_min(env):
    at = T0 + 10 * MIN
    env.broker.submit(ticket(timing="window", winAt=at), "n1")
    env.at(5, BTC=100)
    env.at(9, BTC=101)
    env.at(20, BTC=100.5)
    env.at(39.9, BTC=100.9)
    assert "n1" in env.broker.scheduled
    env.at(40, BTC=100.9)
    b = env.evs("blocked", "n1")[0]
    assert "caduca" in b["text"] and b["patch"]["exec"]["blocked"] is True and b["patch"]["exec"]["scheduled"] == at
    assert not env.broker.scheduled and not env.broker.positions


def test_ventana_espera_si_el_diferencial_es_alto(env):
    at = T0 + 10 * MIN
    env.broker.submit(ticket(timing="window", winAt=at), "n1")
    env.at(5, BTC=100)
    env.at(10, BTC=100)
    env.spread["BTC"] = 7.0
    env.at(11, BTC=100.5)
    assert "n1" in env.broker.scheduled and env.broker.scheduled["n1"]["wait"].startswith("Diferencial")
    env.spread["BTC"] = 1.0
    env.at(12, BTC=100.6)
    assert "n1" in env.broker.positions


def test_ventana_con_el_bot_llegando_tarde_mide_el_rango_desde_entonces(env):
    at = T0 + 10 * MIN
    env.broker.submit(ticket(timing="window", winAt=at), "n1")
    env.at(12, BTC=100)         # primer precio después de winAt: rango hasta el minuto 17
    env.at(15, BTC=100.5)
    assert "n1" in env.broker.scheduled
    env.at(17, BTC=100.4)
    env.at(18, BTC=100.6)
    assert "n1" in env.broker.positions


# ---------------------------------------------------------------- persistencia

def test_persistencia_y_reinicio(env):
    env.broker.submit(ticket(stop=2, trail=1), "n1", "Titular", "seguir-bolsa-anterior", "Apertura Londres")
    env.at(1, BTC=102)
    sl = env.broker.positions["n1"]["sl"]
    env.broker.submit(ticket(coin="ETH", timing="window", winAt=T0 + 60 * MIN), "n2")
    b2 = env.new()
    assert b2.positions["n1"]["sl"] == pytest.approx(sl) and b2.positions["n1"]["be"]
    assert "n2" in b2.scheduled and b2.scheduled["n2"]["rng"] is None
    env.broker = b2
    env.at(2, BTC=99)
    assert not b2.positions and len(b2.closed) == 1 and b2.closed[0]["reason"] == "Stop dinámico"
    b3 = env.new()
    assert not b3.positions and len(b3.closed) == 1 and "n2" in b3.scheduled
    res = b3.results()
    assert res["trades"][0]["story"] == "n1" and res["equity"] == pytest.approx(b2.equity(), abs=0.01)
    rows = env.store.db.execute("SELECT id, story, opened, closed FROM trades").fetchall()
    assert len(rows) == 1 and rows[0][1] == "n1" and rows[0][3] == T0 + 2 * MIN


def test_pendiente_que_caduca_con_el_bot_parado_no_se_aprueba(env):
    env.broker.set_mode("veto")
    env.broker.submit(ticket(), "n1")
    env.clock.t = T0 + 5 * MIN
    env.broker = env.new()
    env.broker.step()
    assert env.evs("humanko", "n1")[0]["patch"]["human"]["by"] == "Caducada con el bot parado"
    assert not env.broker.positions


def test_registros_guardados_ilegibles_no_tumban_el_arranque(env, caplog):
    db = env.store.db
    db.execute("INSERT INTO trades VALUES (?,?,?,?,?)", ("tX", "nX", T0, None, "{roto"))
    db.execute("INSERT INTO trades VALUES (?,?,?,?,?)", ("tY", "nY", T0, None, json.dumps({"story": "nY", "coin": "BTC"})))
    db.commit()
    env.broker._set_state("queue", {"pending": [{"story": "nZ"}], "scheduled": [{"story": "nW", "ticket": {}}]})
    with caplog.at_level(logging.ERROR, logger="paper"):
        b = env.new()
    assert not b.positions and not b.pending and not b.scheduled
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 4
    b.step()
    assert b.portfolio()["positions"] == []


def test_modo_y_capital_inicial_se_recuerdan_tras_reiniciar(env, monkeypatch):
    env.broker.set_mode("manual")
    assert env.new().mode == "manual"                 # no vuelve en silencio a «auto»
    monkeypatch.setattr(config, "APPROVAL_MODE", "veto")
    assert env.new().mode == "veto"                   # si cambias la configuración después, manda la configuración

    monkeypatch.setattr(config, "PAPER_EQUITY", 12000.0)
    env.broker = env.new()
    assert env.broker.start_equity == 12000           # sin operaciones todavía, el capital sale de la configuración
    env.broker.set_mode("auto")
    assert env.new().mode == "auto"
    assert env.broker.submit(ticket(), "n1") and "n1" in env.broker.positions
    monkeypatch.setattr(config, "PAPER_EQUITY", 50000.0)
    b = env.new()
    assert b.start_equity == 12000 and b.portfolio()["start_equity"] == 12000   # con operaciones, no se reescribe


def test_cartera_de_prueba_manda_sobre_la_configuracion(env, monkeypatch):
    """scripts/nueva_cartera_paper.py deja paper_state.cuenta: su capital vale aunque BOT_PAPER_EQUITY diga otra cosa
    (el acceso directo arranca sin esa variable) y la cuenta sale en la cartera y en los resultados."""
    cuenta = {"nombre": "Cartera de prueba de 100 €", "eur": 100.0, "eur_usdc": 1.1137, "usdc": 111.37, "desde": T0, "ficticia": True}
    env.store.db.execute("INSERT OR REPLACE INTO paper_state VALUES ('cuenta', ?)", (json.dumps(cuenta),))
    env.store.db.commit()
    monkeypatch.setattr(config, "PAPER_EQUITY", 10000.0)
    b = env.new()
    assert b.start_equity == 111.37 and b.equity() == 111.37
    assert b.portfolio()["cuenta"] == cuenta and b.results()["cuenta"] == cuenta
    assert env.new().start_equity == 111.37   # y se queda guardado en start_equity
    env.store.db.execute("DELETE FROM paper_state WHERE key='cuenta'")
    env.store.db.commit()
    assert env.new().portfolio()["cuenta"] is None


# ---------------------------------------------------------------- formato

def test_formato_de_eventos_cartera_y_resultados(env):
    env.bus.publish({"type": "event", "kind": "triaged", "agent": "local", "story": "n1", "title": "t", "text": "",
                     "coins": ["BTC"], "patch": {"triage": {"importancia": 7, "credibilidad": 80}}})
    env.broker.submit(ticket(stop=2, trail=1, tp2=3), "n1", "La SEC aprueba algo", "seguir-bolsa-anterior", "Apertura Londres")
    env.broker.submit(ticket(coin="ETH", side="short", stop=2), "n2", "Hackeo", "noticias", "Apertura Nueva York")
    env.broker.submit(ticket(coin="SOL", timing="window", winAt=T0 + 90 * MIN), "n3", "Algo", "noticias", "Apertura Tokio")
    env.at(1, BTC=102.2, ETH=101)
    assert env.broker.publish_portfolio() and not env.broker.publish_portfolio()
    pf = env.bus.state["portfolio"]
    assert {"type", "ts", "equity", "start_equity", "day_pnl", "day_pnl_pct", "positions", "pending", "scheduled",
            "closed", "limits"} <= set(pf)
    assert POS_KEYS <= set(pf["positions"][0]) and pf["scheduled"] == [
        {"story": "n3", "at": T0 + 90 * MIN, "expires": T0 + 120 * MIN, "coin": "SOL", "side": "long"}]
    assert set(pf["limits"]) >= {"risk_per_trade_pct", "max_daily_loss_pct", "max_positions", "day_loss_pct"}
    env.at(2, BTC=106.5, ETH=102.5)   # n1 al objetivo, n2 al stop
    assert env.broker.publish_portfolio()
    pf = env.bus.state["portfolio"]
    assert len(pf["closed"]) == 2 and TRADE_KEYS <= set(pf["closed"][0])

    for e in env.evs():
        if e["kind"] not in AGENT:   # el triaje simulado no es del broker
            continue
        assert EVENT_KEYS <= set(e) and e["agent"] == AGENT[e["kind"]] and PATCH[e["kind"]] in e["patch"]
        assert e["coins"] == [{"n1": "BTC", "n2": "ETH", "n3": "SOL"}[e["story"]]]
    f = env.evs("filled", "n1")[0]["patch"]["exec"]
    assert {"px", "slip", "ms", "notional", "sl", "tp1", "tp2", "trail", "sent"} <= set(f)
    for story in ("n1", "n2"):
        c = env.evs("closed", story)[0]
        assert set(c["patch"]["result"]) == RESULT_KEYS and c["tone"] in ("good", "bad")
        assert re.search(r"[+-]\d+,\d{2} R · comisiones \d+,\d{2} USDC", c["text"])
    assert env.evs("closed", "n1")[0]["tone"] == "good" and env.evs("closed", "n2")[0]["tone"] == "bad"

    res = env.broker.results()
    assert {"start_equity", "equity", "trades", "by_strategy", "by_window", "by_coin", "equity_curve", "daily"} <= set(res)
    t1 = next(t for t in res["trades"] if t["story"] == "n1")
    assert TRADE_KEYS <= set(t1) and t1["id"].startswith("t") and t1["market"] == "londres"
    assert t1["news_score"] == {"importancia": 7, "credibilidad": 80} and t1["reason"] == "Objetivo (3,0 R)"
    assert next(t for t in res["trades"] if t["story"] == "n2")["market"] == "nuevayork"
    assert {r["strategy"] for r in res["by_strategy"]} == {"seguir-bolsa-anterior", "noticias"}
    assert set(res["by_strategy"][0]) == {"strategy", "n", "win_rate", "pnl", "avg_R"}
    assert {"window", "n", "win_rate", "pnl"} <= set(res["by_window"][0])
    assert {"coin", "n", "win_rate", "pnl"} <= set(res["by_coin"][0])
    total = sum(t["pnl"] for t in res["trades"])
    assert res["equity_curve"][0] == [T0, 10000.0] and res["equity_curve"][-1][1] == pytest.approx(10000 + total, abs=0.01)
    [day] = res["daily"]
    assert day["date"] == "2026-10-23" and day["n"] == 2 and day["pnl_pct"] == pytest.approx(total / 100, abs=1e-3)


# ---------------------------------------------------------------- bucle

async def test_bucle_enciende_agentes_y_publica_la_cartera(env, monkeypatch):
    monkeypatch.setattr(paper, "STEP_EVERY_S", 0.01)
    env.broker.submit(ticket(), "n1")
    task = asyncio.create_task(env.broker.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert env.bus.state["agent:exec"]["patch"]["off"] is False and env.bus.state["agent:risk"]["patch"]["now"]
    pf = env.bus.state["portfolio"]
    assert pf["positions"][0]["story"] == "n1" and pf["mode"] == "auto"


def test_un_registro_que_falla_no_frena_al_resto(env, caplog):
    env.broker.submit(ticket(), "n1")
    env.broker.submit(ticket(coin="ETH"), "n2")
    env.broker.positions["n1"]["hold_until"] = None   # registro roto: falla en cada ronda
    with caplog.at_level(logging.ERROR, logger="paper"):
        env.at(1, ETH=97.5)
        env.at(2)
    assert env.result("n2")["why"] == "Stop de pérdida"   # la otra posición se sigue gestionando
    assert "n1" in env.broker.positions
    errs = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errs) == 1 and "n1" in errs[0].getMessage()   # se registra una vez, no cada segundo


async def test_la_cartera_se_publica_aunque_falle_la_ronda(env, monkeypatch, caplog):
    monkeypatch.setattr(paper, "STEP_EVERY_S", 0.01)
    env.broker.submit(ticket(), "n1")

    def roto(now=None):
        raise RuntimeError("fallo de prueba")

    monkeypatch.setattr(env.broker, "step", roto)
    with caplog.at_level(logging.ERROR, logger="paper"):
        task = asyncio.create_task(env.broker.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert env.bus.state["portfolio"]["positions"][0]["story"] == "n1"
    assert len([r for r in caplog.records if "ronda de gestión" in r.getMessage()]) == 1


# ---------------------------------------------------------------- revisión: aprobación tardía y salidas combinadas

def test_ticket_del_motor_aprobado_tarde_no_se_ejecuta(env):
    env.broker.set_mode("veto")
    env.broker.submit(ticket(maxAgeMs=20_000), "n1")
    env.at(0.5)                                              # sin veto en 30 s: aprobada, pero el plan ya es viejo
    b = env.evs("blocked", "n1")[0]
    assert "n1" not in env.broker.positions and b["patch"]["exec"]["blocked"]
    assert b["text"] == "Aprobada 30 s después de enviarla (máximo 20 s): la señal y el plan de salidas ya son viejos"
    env.broker.set_mode("manual")
    env.broker.submit(ticket(coin="ETH", maxAgeMs=60_000), "n2")
    env.broker.submit(ticket(coin="SOL", maxAgeMs=60_000), "n3")
    env.at(1)
    assert env.broker.decide("n2", True) and "n2" in env.broker.positions     # a tiempo (60 s)
    env.at(2.5)
    assert env.broker.decide("n3", True) and "n3" not in env.broker.positions
    assert "Aprobada 2 min después de enviarla (máximo 60 s)" in env.evs("blocked", "n3")[0]["text"]
    env.broker.submit(ticket(coin="XRP"), "n4")              # sin maxAgeMs (fase 2): como siempre
    env.at(5)
    assert env.broker.decide("n4", True) and "n4" in env.broker.positions


def _bars(seq):
    """Velas de 1 min desde T0: (apertura, máximo, mínimo, cierre)."""
    return [[T0 + k * MIN, o, h, l, c, 10.0, 1000.0, 5.0] for k, (o, h, l, c) in enumerate(seq)]


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("case", ["be", "dinamico"])
def test_parcial_con_break_even_o_stop_dinamico_igual_en_paper_y_en_simulate_plan(env, side, case):
    """Mitad en +1 R y el resto al break-even (a +1,5 R) o al stop dinámico: mismos números en el paper y en
    apertura.simulate_plan (comisiones sobre lo negociado y R sobre el riesgo inicial)."""
    from bot import apertura
    d = 1 if side == "long" else -1
    px = lambda x: 100 + d * x
    hi = lambda fav, adv: max(fav, adv)
    lo = lambda fav, adv: min(fav, adv)
    if case == "be":
        plan = {"stopPct": 2, "trailPct": None, "tp1R": 1.0, "tp1Frac": 0.5, "beR": 1.5, "tp2R": 3}
        path = [(px(0), px(2.5), px(-0.1)), (px(2.5), px(3.2), px(2.4)), (px(0.2), px(0.3), px(0.1))]
        ex, why = px(0.2), "Stop en break-even tras salida parcial"
    else:
        plan = {"stopPct": 2, "trailPct": 1.5, "tp1R": 1.0, "tp1Frac": 0.5, "beR": None, "tp2R": None}
        path = [(px(0), px(1), px(-0.1)), (px(1), px(2.5), px(1.1)), (px(2.5), px(4), px(2.6)),
                (px(2.3), px(2.4), px(2.2))]
        ex, why = px(2.3), "Stop dinámico tras salida parcial"
    tp1 = px(2)
    exp = (0.5 * d * (tp1 - 100) + 0.5 * d * (ex - 100)) / 100 - FEE * (1 + 0.5 * tp1 / 100 + 0.5 * ex / 100)
    env.broker.submit(ticket(side=side, stop=2, trail=plan["trailPct"], tp2=plan["tp2R"], **{
        k: plan[k] for k in ("tp1R", "tp1Frac", "beR")}), "n1")
    q = _qty(env)
    for k, (o, fav, adv) in enumerate(path[:-1]):          # en el paper, cada vela: favorable y luego contraria
        env.at(k + 0.5, BTC=fav)
        env.at(k + 0.9, BTC=adv)
    env.at(len(path), BTC=ex)                               # la última abre ya por debajo del stop: sale ahí
    r = env.result("n1")
    assert r["why"] == why and r["pnl_pct"] == pytest.approx(exp * 100, abs=1e-4)   # pnl_pct va redondeado
    assert r["R"] == pytest.approx(exp * 100 / 2, abs=1e-4) and r["qty"] == pytest.approx(q)
    assert r["fees"] == pytest.approx(q * 100 * FEE + q / 2 * tp1 * FEE + q / 2 * ex * FEE, abs=1e-4)
    bars = _bars([(o, hi(fav, adv), lo(fav, adv), adv) for o, fav, adv in path[:-1]]
                 + [(path[-1][0], hi(*path[-1][1:]), lo(*path[-1][1:]), path[-1][0])])
    sim = apertura.simulate_plan(bars, 0, d, plan, T0 + 60 * MIN, FEE, 0.0)
    assert sim["partial"] and sim["first"] == "stop" and sim["why"] == case
    assert sim["ret"] == pytest.approx(exp * 100, abs=1e-9) and sim["R"] == pytest.approx(r["R"], abs=1e-4)


# ---------------------------------------------------------------- mandos de la web: parada, cancelación y modo

def test_parada_manual_veta_lo_nuevo_y_sigue_gestionando_lo_abierto(env):
    b = env.broker
    assert b.submit(ticket(stop=2), "n1")                  # abierta antes de la parada
    assert b.halt("Detenida desde la web") and not b.halt("otra vez")
    pf = env.bus.state["portfolio"]                        # sale al momento, sin esperar a la ronda de 2 s
    assert pf["halted"] is True and pf["caps"] == ["halt", "cancel"] and pf["limits"]["halted"] is False
    for agent in ("risk", "human", "exec"):
        assert env.bus.state[f"agent:{agent}"]["patch"]["now"].startswith("Paper detenido por ti")
    assert not b.submit(ticket(coin="ETH"), "n2")
    ko = env.evs("riskko", "n2")[0]
    assert ko["patch"]["risk"]["why"] == "Paper detenido por ti" and ko["text"] == "Vetada: Paper detenido por ti"
    env.at(1, BTC=97.9)                                    # el stop de lo abierto sigue funcionando
    assert env.result("n1")["why"] == "Stop de pérdida"

    b2 = env.new()                                         # sigue detenido tras reiniciar, y lo dice al arrancar
    assert b2.halted["why"] == "Detenida desde la web" and b2.portfolio()["halted"] is True
    b2.announce()
    assert env.bus.state["agent:human"]["patch"]["now"].startswith("Paper detenido por ti")
    assert b.resume("Reanudada desde la web") and not b.resume("otra vez")
    assert env.bus.state["portfolio"]["halted"] is False and not env.new().halted
    assert env.bus.state["agent:risk"]["patch"]["now"].startswith("Límites:")
    assert b.submit(ticket(coin="ETH"), "n3") and "n3" in b.positions


def test_parada_manual_no_ejecuta_pendientes_ni_programadas(env):
    b = env.broker
    assert b.submit(ticket(coin="ETH", timing="window", winAt=T0 + 10 * MIN), "n2")   # programada (auto)
    b.set_mode("manual")
    assert b.submit(ticket(coin="SOL"), "n3")              # espera tu aprobación
    b.set_mode("veto")
    assert b.submit(ticket(), "n1")                        # espera el plazo de veto
    b.halt("Detenida desde la web")
    env.at(0.5)                                            # vence el veto: con el paper detenido no se aprueba sola
    assert env.evs("humanko", "n1")[0]["patch"]["human"]["by"] == "Paper detenido por ti: no se aprueba sola"
    assert b.decide("n3", True)                            # aprobada a mano: tampoco entra
    assert env.kinds("n3") == ["riskok", "humanwait", "humanok", "blocked"]
    assert env.evs("blocked", "n3")[0]["patch"]["exec"]["why"] == "Paper detenido por ti"
    env.at(5, ETH=100)
    env.at(10, ETH=100)
    env.at(11, ETH=101)                                    # rompe el rango, pero espera mientras siga detenido
    assert "n2" in b.scheduled and b.scheduled["n2"]["wait"] == "Paper detenido por ti" and not b.positions
    b.resume("Reanudada desde la web")
    env.at(12, ETH=101.2)                                  # reanudado dentro de su plazo: entra
    assert "n2" in b.positions


def test_programada_que_caduca_con_el_paper_detenido(env):
    b = env.broker
    assert b.submit(ticket(timing="window", winAt=T0 + 10 * MIN), "n1")
    b.halt("Detenida desde la web")
    for m, px in ((5, 100), (10, 100), (12, 101), (40, 102)):
        env.at(m, BTC=px)
    blk = env.evs("blocked", "n1")[0]
    assert blk["text"] == "Paper detenido por ti: la entrada programada caduca sin ejecutarse"
    assert not b.scheduled and not b.positions


def test_cancelar_pendiente_o_programada_sin_tocar_lo_abierto(env):
    b = env.broker
    assert b.submit(ticket(), "n1")                                                  # abierta
    assert b.submit(ticket(coin="ETH", timing="window", winAt=T0 + 60 * MIN), "n2")  # programada
    b.set_mode("manual")
    assert b.submit(ticket(coin="SOL"), "n3")                                        # pendiente de aprobación
    assert b.cancel("n2", "Cancelada por ti desde la web") and b.cancel("n3")
    blk = env.evs("blocked", "n2")[0]
    assert blk["text"] == "Cancelada por ti desde la web" and blk["patch"]["exec"]["blocked"] is True
    ko = env.evs("humanko", "n3")[0]["patch"]["human"]
    assert ko["by"] == "Cancelada por ti" and ko["no"] is True and ko["mode"] == "manual"
    pf = env.bus.state["portfolio"]                        # la web lo ve al momento
    assert not pf["scheduled"] and not pf["pending"] and [p["story"] for p in pf["positions"]] == ["n1"]
    assert not b.cancel("n1") and "n1" in b.positions     # lo abierto no se cancela: se cierra con close
    assert not b.cancel("n2") and not b.cancel("no-existe")
    b2 = env.new()
    assert not b2.scheduled and not b2.pending             # tampoco vuelven tras reiniciar
    env.at(61, ETH=110)
    assert set(b.positions) == {"n1"}


def test_pasar_de_veto_a_manual_no_aprueba_sola_lo_pendiente(env):
    b = env.broker
    b.set_mode("veto")
    assert b.submit(ticket(), "n1")
    env.at(0.1)
    assert b.set_mode("manual")
    it = b.pending["n1"]
    assert it["mode"] == "manual" and it["deadline"] == T0 + int(0.1 * MIN) + 10 * MIN
    w = env.evs("humanwait", "n1")[-1]
    assert w["patch"]["human"] == {"mode": "manual", "deadline": it["deadline"]} and "ya no se aprueba sola" in w["text"]
    pf = env.bus.state["portfolio"]
    assert pf["mode"] == "manual" and pf["pending"][0]["mode"] == "manual"
    env.at(1)                                              # pasado el plazo del veto: sigue esperando
    assert "n1" in b.pending and "n1" not in b.positions
    assert env.new().pending["n1"]["mode"] == "manual"     # guardado así
    assert b.decide("n1", True) and "n1" in b.positions


def test_cerrar_y_decidir_publican_la_cartera_al_momento(env):
    b = env.broker
    b.set_mode("manual")
    assert b.submit(ticket(), "n1")
    assert b.decide("n1", True)
    assert [p["story"] for p in env.bus.state["portfolio"]["positions"]] == ["n1"]
    assert b.close("n1", "Cierre manual desde la web")
    assert env.bus.state["portfolio"]["positions"] == [] and env.bus.state["portfolio"]["closed"][0]["story"] == "n1"


async def _portfolio(ws, ok, timeout=3.0):
    """Lee el WebSocket hasta un «portfolio» que cumpla ok (los mensajes pueden llegar en lotes)."""
    async def read():
        while True:
            data = json.loads((await ws.receive()).data)
            for m in data if isinstance(data, list) else [data]:
                if m.get("type") == "portfolio" and ok(m):
                    return m
    return await asyncio.wait_for(read(), timeout)


async def test_mandos_por_el_websocket_con_el_paper_real(env):
    from aiohttp.test_utils import TestClient, TestServer
    from bot.server import Services, make_app
    b = env.broker
    b.set_mode("manual")
    assert b.submit(ticket(), "n1")
    async with TestClient(TestServer(make_app(env.bus, lambda: {}, Services(broker=b)))) as client:
        ws = await client.ws_connect("/events", origin=f"http://127.0.0.1:{config.PORT}")
        for m in ({"type": "decision", "story": "n1", "ok": "true"}, {"type": "halt"}, {"type": "cancel", "story": "n1"}):
            await ws.send_str(json.dumps(m))
        pf = await _portfolio(ws, lambda p: p["halted"] and not p["pending"])
        assert pf["caps"] == ["halt", "cancel"] and not pf["positions"]   # la cadena "true" no aprobó nada
        await ws.send_str(json.dumps({"type": "resume"}))
        await _portfolio(ws, lambda p: p["halted"] is False)
        await ws.close()
    assert env.evs("humanko", "n1")[0]["patch"]["human"]["by"] == "Cancelada por ti desde la web"
    assert not b.halted and not env.evs("humanok", "n1")


# ---------------------------------------------------------------- reglas del usuario (stop ≥ 1,5 %, ≥ 2R, horquilla pura)

def test_reglas_del_usuario_rechazan_stop_corto_y_objetivo_pequeno(env, monkeypatch):
    monkeypatch.setattr(config, "USER_RULES", True)
    assert not env.broker.submit(ticket(stop=1.0, tp2=3.0), "n1")
    assert "Regla del usuario: stop" in env.evs("riskko", "n1")[0]["patch"]["risk"]["why"]
    assert not env.broker.submit(ticket(stop=2.0, tp2=1.5), "n2")
    assert "objetivo" in env.evs("riskko", "n2")[0]["patch"]["risk"]["why"]
    assert not env.broker.submit(ticket(stop=2.0, tp2=None), "n3")   # sin objetivo fijo no hay 2:1
    assert env.broker.submit(ticket(stop=2.0, tp2=3.0, trail=0.5, beR=1.0), "n4")


def test_reglas_del_usuario_dan_horquilla_pura_sin_break_even_ni_dinamico(env, monkeypatch):
    monkeypatch.setattr(config, "USER_RULES", True)
    assert env.broker.submit(ticket(stop=2.0, tp2=2.0, trail=0.5, beR=1.0, tp1Frac=0.5), "n1")
    env.at(1, BTC=103.0)   # +1,5 R: sin las reglas, el break-even y el dinámico ya habrían subido el stop
    [p] = env.broker.portfolio()["positions"]
    assert p["sl"] < p["entry"] and not p.get("tp1_done")
    env.at(2, BTC=104.5)   # toca el objetivo de 2R
    r = env.result("n1")
    assert r["R"] > 1.7 and r["pnl"] > 0


def test_make_ticket_aplica_las_reglas(monkeypatch):
    from datetime import datetime, timezone
    from bot.claude_agents import make_ticket
    monkeypatch.setattr(config, "USER_RULES", True)
    t = make_ticket("SOL", "long", "Seguir la tendencia", 0.5, 0.8, 0.4, 1.0, 1.5, "", "", "now", "Apertura Londres",
                    datetime.now(timezone.utc), None, 0.5)
    assert t["stopPct"] == 1.5 and t["tp2R"] == 3.0 and t["trailPct"] is None and t["beR"] is None and t["tp1Frac"] == 0


# ---------------------------------------------------------------- perpetuos: funding y apalancamiento 2:1

def _with_flow(env, flow: dict) -> None:
    """El mercado de mentira con el flujo (funding en % por 8 h) que se va cambiando en la prueba."""
    env.broker.get_market = lambda: {**env.market(), "flow": flow}


def test_liquidaciones_del_funding_y_signo():
    assert paper.next_funding_ms(T0) == T0 + 480 * MIN              # T0 = 08:00 UTC: la siguiente, a las 16:00
    assert paper.next_funding_ms(T0 - 1) == T0
    assert paper.funding_pay("short", 1000.0, 0.01) == pytest.approx(0.1)    # tasa positiva: el corto cobra
    assert paper.funding_pay("long", 1000.0, 0.01) == pytest.approx(-0.1)    # ... y el largo paga
    assert paper.funding_pay("long", 1000.0, -0.02) == pytest.approx(0.2)
    assert paper.instrument_of({"instrument": "perp"}) == "perp" and paper.instrument_of({}) == "spot"
    # Supuesto: la tasa de Binance es de un periodo de 8 h; con funding_h (horas del periodo) se escala a 8 h.
    assert paper.funding_8h({"funding": 0.01}) == 0.01 and paper.funding_8h({"funding": 0.01, "funding_h": 4}) == 0.02
    assert paper.funding_8h({}) is None and paper.funding_8h(None) is None
    assert paper.funding_8h({"funding": 0.01, "funding_h": 0}) == 0.01


def test_reglas_del_usuario_llevan_el_corto_sin_instrumento_a_perpetuo(env, monkeypatch):
    _with_flow(env, {"BTC": {"funding": 0.01}})
    monkeypatch.setattr(config, "USER_RULES", True)
    assert env.broker.submit(ticket(side="short", stop=2.0, tp2=3.0), "n1")   # como los cortos de la fase 2
    checks = env.evs("riskok", "n1")[0]["patch"]["risk"]["checks"]
    assert checks[-1] == {"ok": True, "t": "Corto sin instrumento: va en perpetuo (en contado USDC no se puede ir corto)"}
    assert any(c["t"].startswith("Nominal en perpetuos") for c in checks)
    p = env.broker.positions["n1"]
    assert p["instrument"] == "perp" and p["funding_rate"] == 0.01 and p["funding_n"] == 0
    assert env.broker.submit(ticket(coin="ETH", side="short", stop=2.0, tp2=3.0, instrument="spot"), "n2")
    checks = env.evs("riskok", "n2")[0]["patch"]["risk"]["checks"]
    assert checks[-1]["t"] == "Corto al contado: no se puede ejecutar en OKX EU (USDC), solo se mide en paper"
    assert env.broker.positions["n2"]["instrument"] == "spot"
    assert env.broker.submit(ticket(coin="SOL", stop=2.0, tp2=3.0), "n3")      # el largo sin instrumento, al contado
    assert env.broker.positions["n3"]["instrument"] == "spot"
    monkeypatch.setattr(config, "USER_RULES", False)                         # sin las reglas, como siempre
    assert env.broker.submit(ticket(coin="XRP", side="short", stop=2.0), "n4")
    assert env.broker.positions["n4"]["instrument"] == "spot" and "funding" not in env.broker.positions["n4"]


def test_corto_en_perpetuo_cobra_el_funding_y_entra_en_resultado_r_y_comisiones(env):
    flow = {"BTC": {"funding": 0.01}}
    _with_flow(env, flow)
    tags = {"lado": "corto", "noticia": "neutral"}
    t = ticket(side="short", stop=2, tp2=3.0, instrument="perp", tags=tags, winEnd=T0 + 3 * 1440 * MIN)
    assert env.broker.submit(t, "n1", strategy="impulso-corto-sin-noticia")
    p = env.broker.positions["n1"]
    q = p["qty"]
    assert p["instrument"] == "perp" and p["funding_next"] == T0 + 480 * MIN and p["funding_rate"] == 0.01
    f = env.evs("filled", "n1")[0]
    assert "perpetuo: funding cada 8 h (ahora 0,0100 %)" in f["text"] and f["patch"]["exec"]["instrument"] == "perp"
    env.at(479)                                   # 15:59 UTC: aún no hay liquidación
    assert env.broker.positions["n1"]["funding_n"] == 0
    env.at(481)                                   # 16:00 con 0,01 %: el corto cobra
    p = env.broker.positions["n1"]
    assert p["funding_n"] == 1 and p["funding"] == pytest.approx(q * 100 * 0.0001)
    m = env.evs("manage", "n1")[-1]
    assert m["title"].startswith("BTC: funding cobrado +") and m["title"].endswith(" USDC (paper)")
    assert "tasa 0,0100 % por 8 h" in m["text"]
    [pp] = env.broker.portfolio()["positions"]
    assert pp["instrument"] == "perp" and pp["funding"] == pytest.approx(q * 0.01, abs=0.01) and pp["tags"] == tags
    env.broker = env.new()                        # tras reiniciar sigue con su funding y la última tasa conocida
    flow.clear()
    _with_flow(env, flow)
    env.at(961)                                   # 00:00 del 24 sin dato de tasa: la última conocida (0,01 %)
    assert env.broker.positions["n1"]["funding_n"] == 2
    flow["BTC"] = {"funding": -0.005}             # tasa negativa: el corto paga
    env.at(1441)
    fund = q * 100 * (0.0001 + 0.0001 - 0.00005)
    assert env.broker.positions["n1"]["funding"] == pytest.approx(fund)
    env.at(1442, BTC=94.0)                        # objetivo 3R (6 %)
    r, t = env.result("n1"), env.broker.closed[-1]
    comm = q * 100 * FEE + q * 94 * FEE
    assert r["why"] == "Objetivo (3,0 R)" and t["instrument"] == "perp" and t["funding"] == pytest.approx(fund, abs=1e-4)
    assert t["fees"] == pytest.approx(comm - fund, abs=1e-4) and t["commissions"] == pytest.approx(comm, abs=1e-4)
    assert r["pnl"] == pytest.approx(6 * q - comm + fund, abs=1e-3) and r["fees"] == t["fees"]
    assert r["R"] == pytest.approx(r["pnl"] / p["riskUsd"], abs=1e-3) and t["tags"] == tags
    assert "con funding +" in env.evs("closed", "n1")[0]["text"]
    [row] = env.broker.results()["by_strategy"]
    assert row["strategy"] == "impulso-corto-sin-noticia" and row["n"] == 1


def test_largo_en_perpetuo_paga_el_funding_y_al_contado_no(env):
    _with_flow(env, {"BTC": {"funding": 0.03}, "ETH": {"funding": 0.03}})
    assert env.broker.submit(ticket(stop=2, tp2=3.0, instrument="perp", winEnd=T0 + 1440 * MIN), "n1")
    assert env.broker.submit(ticket(coin="ETH", stop=2, tp2=3.0, winEnd=T0 + 1440 * MIN), "n2")   # sin instrument: contado
    env.at(481)
    p1, p2 = env.broker.positions["n1"], env.broker.positions["n2"]
    assert p1["funding"] == pytest.approx(-p1["qty"] * 100 * 0.0003) and p2["instrument"] == "spot" and "funding" not in p2
    eq = 10000 - p1["fee_open"] - p2["fee_open"] + p1["funding"]   # el funding cuenta en el capital al momento
    assert env.broker.portfolio()["equity"] == pytest.approx(eq, abs=0.01)
    env.at(482, BTC=98.0)                         # stop del largo
    t = env.broker.closed[-1]
    comm = p1["qty"] * (100 + 98) * FEE
    assert t["funding"] < 0 and t["fees"] == pytest.approx(comm - t["funding"], abs=1e-4)
    assert t["pnl"] == pytest.approx(-2 * p1["qty"] - comm + t["funding"], abs=1e-3)


def test_perpetuos_cuentan_su_margen_y_no_pasan_de_2_a_1(env, monkeypatch):
    monkeypatch.setattr(paper, "MAX_NOTIONAL_PCT", 90.0)
    b = env.broker
    assert b.submit(ticket(stop=0.2, instrument="perp"), "n1")                     # 90 % de nominal
    assert b.submit(ticket(coin="ETH", side="short", stop=0.2, instrument="perp"), "n2")   # exposición 90 % (margen)
    checks = env.evs("riskok", "n2")[0]["patch"]["risk"]["checks"]
    assert len(checks) == 9 and checks[-1]["ok"]
    assert re.fullmatch(r"Nominal en perpetuos 18\d % ≤ 200 % del capital \(2:1, máximo para particulares en España\)",
                        checks[-1]["t"])
    assert not b.submit(ticket(coin="SOL", stop=0.2, instrument="perp"), "n3")
    assert re.fullmatch(r"Exposición total 13\d % > 100 % del capital \(los perpetuos, por su margen: la mitad del "
                        r"nominal\)", _why(env, "n3"))
    monkeypatch.setattr(paper, "MAX_EXPOSURE_PCT", 300.0)                          # con más margen manda el 2:1
    assert not b.submit(ticket(coin="SOL", stop=0.2, instrument="perp"), "n4")
    assert re.fullmatch(r"Nominal en perpetuos 2[67]\d % > 200 % del capital \(2:1, .*\)", _why(env, "n4"))
    assert not b.submit(ticket(coin="XRP", stop=1, instrument="futuro"), "n5")
    assert _why(env, "n5") == "Instrumento desconocido: futuro (spot o perp)"


def test_reglas_del_usuario_aceptan_el_ticket_del_modo_impulso(env, monkeypatch):
    monkeypatch.setattr(config, "USER_RULES", True)
    t = ticket(side="short", stop=3.0, tp2=3.0, tp1R=3.0, tp1Frac=0.0, beR=None, trailStartR=None, instrument="perp",
               winEnd=T0 + 168 * 60 * MIN, holdLabel="7 días de seguridad", maxAgeMs=60_000)
    assert env.broker.submit(t, "a-europa-2026-10-23-btc-1", strategy="impulso-corto-sin-noticia")
    p = env.broker.positions["a-europa-2026-10-23-btc-1"]
    assert p["hold_until"] == T0 + 168 * 60 * MIN and p["hold_label"] == "7 días de seguridad" and p["be_r"] is None
    assert p["tp2"] == pytest.approx(91.0) and p["sl"] == pytest.approx(103.0) and p["trail_pct"] is None


# ---------------------------------------------------------------- huecos: posiciones sin gestionar (hallazgo n.º 47)

def velas(desde_min: int, hasta_min: int, px=100.0, at=None) -> list[list]:
    """Velas de 1 min planas en px de T0 + desde_min a T0 + hasta_min; at = {minuto: (o, h, l, c)} las cambia."""
    out = []
    for k in range(desde_min, hasta_min):
        o, h, l, c = (at or {}).get(k, (px, px, px, px))
        out.append([T0 + k * MIN, o, h, l, c, 1.0])
    return out


def test_replay_gap_puro():
    sl, tp = 98.0, 106.0
    flat = velas(0, 10)
    assert paper.replay_gap("long", sl, tp, None, flat)["salida"] is None
    r = paper.replay_gap("long", sl, tp, None, velas(0, 10, at={3: (100, 100.5, 97.5, 99)}))
    assert r["salida"] == "stop" and r["px"] == sl and r["at"] == T0 + 3 * MIN and r["n"] == 4
    r = paper.replay_gap("long", sl, tp, None, velas(0, 10, at={5: (100, 107, 99, 105)}))
    assert r["salida"] == "objetivo" and r["px"] == tp and r["at"] == T0 + 5 * MIN and r["best"] == 107
    # toca los dos en la misma vela: el stop (el peor caso)
    assert paper.replay_gap("long", sl, tp, None, velas(0, 10, at={2: (100, 107, 97, 100)}))["salida"] == "stop"
    # la apertura salta el stop: sale a la apertura (peor que el stop)
    r = paper.replay_gap("long", sl, tp, None, velas(0, 10, at={4: (96, 96.5, 95, 96)}))
    assert r["salida"] == "stop" and r["px"] == 96
    # tope de tiempo cumplido sin tocar nada: sale a la apertura de la vela de esa hora
    r = paper.replay_gap("long", sl, tp, T0 + 6 * MIN + 30_000, velas(0, 10, px=101))
    assert r["salida"] == "tiempo" and r["px"] == 101 and r["at"] == T0 + 7 * MIN
    # corto: el stop está arriba
    r = paper.replay_gap("short", 102.0, 94.0, None, velas(0, 10, at={1: (100, 102.5, 99, 100)}))
    assert r["salida"] == "stop" and r["px"] == 102.0
    # velas de Binance en USDT con un 0,1 % de base sobre OKX en USDC: se pasan a la escala de OKX
    r = paper.replay_gap("long", sl, tp, None, velas(0, 5, px=100.1, at={2: (100.1, 100.1, 98.05, 100.1)}),
                         factor=100 / 100.1)
    assert r["salida"] == "stop"   # 98,05 USDT son 97,95 USDC: por debajo del stop de 98
    r2 = paper.replay_gap("long", sl, tp, None, velas(0, 5, px=100.1, at={2: (100.1, 100.1, 98.15, 100.1)}),
                          factor=100 / 100.1)
    assert r2["salida"] is None
    assert paper.gap_complete(velas(0, 60), T0, T0 + 60 * MIN)
    assert not paper.gap_complete(velas(0, 30) + velas(40, 60), T0, T0 + 60 * MIN)
    assert not paper.gap_complete([], T0, T0 + 60 * MIN)


class Velas:
    """fetch_bars de mentira: velas de 1 min por moneda; falla las primeras `fallos` veces."""

    def __init__(self, series: dict, fallos: int = 0):
        self.series, self.fallos, self.calls = series, fallos, []

    async def __call__(self, coin, a, b):
        self.calls.append((coin, a, b))
        if self.fallos:
            self.fallos -= 1
            raise OSError("sin red")
        return [r for r in self.series.get(coin, []) if a <= r[0] < b]


def con_velas(env, fetch) -> PaperBroker:
    return PaperBroker(env.bus, env.store, env.market, lambda: {"coins": {}}, clock=env.clock,
                       slip_bps=lambda: env.slip, fetch_bars=fetch)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_hueco_con_el_bot_parado_cierra_en_el_stop_y_a_su_hora(env, monkeypatch):
    monkeypatch.setattr(config, "USER_RULES", True)
    env.broker.submit(ticket(stop=2.0, tp2=3.0, winEnd=T0 + 24 * 60 * MIN), "n1", "Titular", "impulso", "Apertura Londres")
    env.at(1, BTC=100.0)
    p = env.broker.positions["n1"]
    sl, entry = p["sl"], p["entry"]
    assert p["seen"] == T0 + MIN
    # el bot se para en el minuto 1 y vuelve en el 180: en el 95 el precio tocó el stop
    fetch = Velas({"BTC": velas(-5, 180, at={95: (99, 99, 97.5, 98)})})
    env.clock.t = T0 + 180 * MIN
    env.broker = con_velas(env, fetch)
    env.broker.step()   # el hueco se ve, pero no se gestiona: ni se cierra al precio de ahora
    assert "n1" in env.broker.positions and env.broker.portfolio()["positions"][0]["reconstruyendo"] is True
    env.prices["BTC"] = 120.0   # aunque ahora el precio esté por encima del objetivo, lo que pasó antes manda
    env.broker.step()
    assert "n1" in env.broker.positions
    assert run(env.broker.reconcile()) == 1 and not env.broker.positions
    t = env.broker.closed[-1]
    assert t["closed"] == T0 + 95 * MIN and t["exit"] == pytest.approx(sl) and t["R"] < -1
    assert "Stop de pérdida con el bot sin gestionarla" in t["reason"] and "Binance" in t["reason"]
    assert t["hueco"]["desde"] == T0 + MIN and t["hueco"]["hasta"] == T0 + 180 * MIN and t["hueco"]["completo"]
    [c] = env.evs("closed", "n1")
    assert c["ts"] == T0 + 180 * MIN and c["patch"]["result"]["closedAt"] == T0 + 95 * MIN
    row = env.store.db.execute("SELECT closed FROM trades WHERE story='n1'").fetchone()
    assert row[0] == T0 + 95 * MIN and entry == pytest.approx(100.0)


def test_hueco_con_el_objetivo_y_con_el_tope_de_tiempo(env, monkeypatch):
    monkeypatch.setattr(config, "USER_RULES", True)
    env.broker.submit(ticket(stop=1.5, tp2=3.0, winEnd=T0 + 24 * 60 * MIN), "n1")
    env.broker.submit(ticket(coin="ETH", side="short", stop=1.5, tp2=3.0, instrument="perp",
                             winEnd=T0 + 120 * MIN), "n2")
    env.at(1)
    tp = env.broker.positions["n1"]["tp2"]
    subida = {k: (100.4, 100.4, 100.4, 100.4) for k in range(100, 300)}
    fetch = Velas({"BTC": velas(-5, 300, at={60: (100, 104.6, 99.9, 104)}), "ETH": velas(-5, 300, at=subida)})
    env.clock.t = T0 + 300 * MIN
    env.broker = con_velas(env, fetch)
    env.broker.step()
    assert run(env.broker.reconcile()) == 2
    by = {t["story"]: t for t in env.broker.closed}
    assert by["n1"]["closed"] == T0 + 60 * MIN and by["n1"]["exit"] == pytest.approx(tp) and "Objetivo" in by["n1"]["reason"]
    assert by["n2"]["closed"] == T0 + 120 * MIN and "Cierre por tiempo" in by["n2"]["reason"]
    assert by["n2"]["exit"] == pytest.approx(100.4)


def test_hueco_sin_salida_sigue_en_vivo_y_lo_apunta(env, monkeypatch):
    monkeypatch.setattr(config, "USER_RULES", True)
    env.broker.submit(ticket(stop=2.0, tp2=3.0, winEnd=T0 + 24 * 60 * MIN), "n1")
    env.at(1)
    fetch = Velas({"BTC": velas(-5, 60, at={30: (101, 103, 100.5, 102)})})
    env.clock.t = T0 + 60 * MIN
    env.broker = con_velas(env, fetch)
    env.broker.step()
    assert run(env.broker.reconcile()) == 0
    p = env.broker.positions["n1"]
    assert p["high_water"] == pytest.approx(103) and p["huecos"][0]["velas"] == 59 and p["seen"] == T0 + 60 * MIN
    assert any("hueco revisado" in e["title"] for e in env.evs("manage", "n1"))
    env.at(61, BTC=97.0)   # y sigue gestionándose en vivo: el stop salta
    assert not env.broker.positions and env.broker.closed[-1]["reason"] == "Stop de pérdida"
    assert env.broker.closed[-1]["huecos"][0]["desde"] == T0 + MIN


def test_hueco_sin_velas_reintenta_y_al_final_sigue_marcado(env, monkeypatch):
    monkeypatch.setattr(config, "USER_RULES", True)
    env.broker.submit(ticket(stop=2.0, tp2=3.0, winEnd=T0 + 24 * 60 * MIN), "n1")
    env.at(1)
    fetch = Velas({"BTC": velas(-5, 60)}, fallos=99)
    env.clock.t = T0 + 60 * MIN
    env.broker = con_velas(env, fetch)
    env.broker.step()
    for k in range(paper.GAP_TRIES):
        assert run(env.broker.reconcile()) == 0
        assert ("n1" in env.broker._gaps) == (k < paper.GAP_TRIES - 1)
        env.clock.t += paper.GAP_RETRY_S * 1000
    assert len(fetch.calls) == paper.GAP_TRIES
    p = env.broker.positions["n1"]
    assert p["huecos"][0]["completo"] is False and p["huecos"][0]["velas"] == 0


def test_posicion_guardada_sin_seen_usa_la_ultima_actividad(env, monkeypatch):
    monkeypatch.setattr(config, "USER_RULES", True)
    env.broker.submit(ticket(stop=2.0, tp2=3.0, winEnd=T0 + 24 * 60 * MIN), "n1")
    env.at(1)
    body = json.loads(env.store.db.execute("SELECT body FROM trades WHERE story='n1'").fetchone()[0])
    body.pop("seen")
    env.store.db.execute("UPDATE trades SET body=? WHERE story='n1'", (json.dumps(body),))
    env.bus.publish({"type": "event", "ts": T0 + 40 * MIN, "kind": "triaged", "agent": "local", "story": "x",
                     "title": "t", "patch": {}})
    env.clock.t = T0 + 100 * MIN
    b = con_velas(env, Velas({"BTC": velas(-5, 100)}))
    assert b.positions["n1"]["seen"] == T0 + 40 * MIN


def test_perpetuo_cobra_el_funding_del_hueco_antes_de_cerrar(env, monkeypatch):
    monkeypatch.setattr(config, "USER_RULES", True)
    t16 = int(datetime(2026, 10, 23, 16, 0, tzinfo=UTC).timestamp() * 1000)   # liquidación de las 16:00 UTC
    k16 = (t16 - T0) // MIN
    env.broker.submit(ticket(side="short", stop=2.0, tp2=3.0, instrument="perp", winEnd=T0 + 24 * 60 * MIN), "n1")
    env.at(1)
    env.broker.positions["n1"]["funding_rate"] = 0.01
    fetch = Velas({"BTC": velas(-5, k16 + 30, at={k16 + 10: (100, 102.5, 100, 102)})})
    env.clock.t = T0 + (k16 + 30) * MIN
    env.broker = con_velas(env, fetch)
    env.broker.positions["n1"]["funding_rate"] = 0.01
    env.broker.step()
    assert run(env.broker.reconcile()) == 1
    t = env.broker.closed[-1]
    assert t["closed"] == t16 + 10 * MIN and t["funding"] > 0   # el corto cobra la liquidación de las 16:00
    assert len([e for e in env.evs("manage", "n1") if "funding" in e["title"]]) == 1


def test_sin_precios_un_rato_con_el_bot_en_marcha_tambien_es_un_hueco(env, monkeypatch):
    monkeypatch.setattr(config, "USER_RULES", True)
    fetch = Velas({"BTC": velas(-5, 30, at={10: (100, 100, 97, 98)})})
    env.broker = con_velas(env, fetch)
    env.updated = {c: T0 for c in config.COINS}
    env.broker.submit(ticket(stop=2.0, tp2=3.0, winEnd=T0 + 24 * 60 * MIN), "n1")
    env.at(20)   # precios de hace 20 min: sin gestionar
    assert "n1" in env.broker.positions and not env.broker._gaps
    env.updated = {c: T0 + 20 * MIN for c in config.COINS}
    env.at(20)   # vuelven los precios: hueco de 19 min
    assert "n1" in env.broker._gaps
    env.clock.t = T0 + 21 * MIN
    assert run(env.broker.reconcile()) == 1 and env.broker.closed[-1]["closed"] == T0 + 10 * MIN
