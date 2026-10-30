import array
import asyncio
import hashlib
import io
import json
import math
import zipfile
from datetime import date, datetime, timezone

import pytest

from bot import apertura as ap
from bot import backtest_aperturas as bt
from bot.apertura import MIN

T0 = int(datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc).timestamp() * 1000)


def bar(t, o, h, l, c, v=10.0, tb=5.0):
    return [t, o, h, l, c, v, v * c, tb]


# ---------------------------------------------------------------- datos

def test_meses_y_limites():
    months, cur = bt.month_list(date(2026, 1, 15), 3)
    assert months == ["2025-10", "2025-11", "2025-12"] and cur == "2026-01"
    a, b = bt.month_bounds("2025-12")
    assert datetime.fromtimestamp(a / 1000, timezone.utc).isoformat() == "2025-12-01T00:00:00+00:00"
    assert datetime.fromtimestamp(b / 1000, timezone.utc).isoformat() == "2026-01-01T00:00:00+00:00"


def test_csv_en_milisegundos_y_microsegundos():
    ms = "1735689600000,93576.00,93610.93,93537.50,93610.93,8.21827,1735689659999,768978.0,1257,3.6,337000.0,0"
    us = "1735689600000000,93576.00,93610.93,93537.50,93610.93,8.21827,1735689659999999,768978.0,1257,3.6,337000.0,0"
    r = bt.parse_csv_line(ms)
    assert r == [1735689600000, 93576.0, 93610.93, 93537.5, 93610.93, 8.21827, 768978.0, 3.6]
    assert bt.parse_csv_line(us) == r
    assert bt.parse_csv_line("open_time,open,high,low,close,volume,close_time,quote_volume,count,tb,tq,ignore") is None


def _zip_bytes(lines: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("X-1m.csv", "\n".join(lines) + "\n")
    return buf.getvalue()


def test_serie_desde_zip_y_api_igual_que_las_funciones_del_motor(tmp_path):
    rows = [bar(T0 + k * MIN, 100 + k, 101 + k, 99 + k, 100.5 + k, 10 + k, 3 + k) for k in range(10)]
    lines = ["open_time,open,high,low,close,volume,close_time,quote_volume,count,tb,tq,ignore"]
    lines += [f"{r[0] * 1000},{r[1]},{r[2]},{r[3]},{r[4]},{r[5]},{r[0] * 1000 + 59_999_999},{r[6]},1,{r[7]},0,0"
              for r in rows[:6]]                                   # microsegundos, como desde 2025
    z = tmp_path / "BTCUSDT-1m-2026-10.zip"
    z.write_bytes(_zip_bytes(lines))
    j = tmp_path / "BTCUSDT-1m-2026-11-api.json"
    j.write_text(json.dumps({"rows": rows[4:]}), encoding="utf-8")   # solapa 2 velas con el zip
    s = bt.load_series([j, z])
    assert len(s) == 10 and list(s.t) == [r[0] for r in rows]
    assert s.slice(T0, T0 + 3 * MIN) == [[float(x) if i else x for i, x in enumerate(r)] for r in rows[:3]]
    for a, b in ((T0 - MIN, T0 + 4 * MIN), (T0 + 2 * MIN, T0 + 9 * MIN), (T0 + 30 * MIN, T0 + 40 * MIN)):
        assert s.vol_sum(a, b) == pytest.approx(ap.slot_volume(rows, a, b))
    assert s.close_before(T0 + 3 * MIN) == 102.5 and s.close_before(T0) is None


class FakeResp:
    def __init__(self, status, body, size=None):
        self.status, self.body = status, body
        self.headers = {"Content-Length": str(len(body) if size is None else size)}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise ConnectionError(f"HTTP {self.status}")

    async def read(self):
        return self.body


class FakeSession:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, timeout=None, params=None):
        self.calls.append(url)
        r = self.routes.get(url)
        return r() if callable(r) else (r or FakeResp(404, b""))


async def _nosleep(_s):
    return None


def test_descarga_verificada_con_suma_y_tamano(tmp_path, monkeypatch):
    monkeypatch.setattr(bt.config, "HIST_DIR", tmp_path)
    monkeypatch.setattr(bt.asyncio, "sleep", _nosleep)
    body = _zip_bytes(["1,1,1,1,1,1,2,1,1,1,1,0"])
    url = bt.ZIP_URL.format(sym="BTCUSDT", ym="2026-09")
    good = hashlib.sha256(body).hexdigest() + "  BTCUSDT-1m-2026-09.zip"
    s = FakeSession({url: FakeResp(200, body), url + ".CHECKSUM": FakeResp(200, good.encode())})
    assert asyncio.run(bt.fetch_zip(s, "BTCUSDT", "2026-09"))
    assert bt.zip_path("BTCUSDT", "2026-09").read_bytes() == body and bt.zip_ok(bt.zip_path("BTCUSDT", "2026-09"))
    bad = FakeSession({url: FakeResp(200, body), url + ".CHECKSUM": FakeResp(200, b"00ff  x.zip")})
    with pytest.raises(IOError):
        asyncio.run(bt.fetch_zip(bad, "BTCUSDT", "2026-09"))
    short = FakeSession({url: lambda: FakeResp(200, body[:-5], size=len(body))})
    with pytest.raises(IOError):
        asyncio.run(bt.fetch_zip(short, "BTCUSDT", "2026-09"))
    assert len(short.calls) == 4                                   # reintentos con espera creciente
    assert asyncio.run(bt.fetch_zip(FakeSession({}), "BTCUSDT", "2026-10")) is False   # 404: aún no publicado


# ---------------------------------------------------------------- aperturas y señales

def _idx(dates_by_market: dict, first="2026-01-01", last="2026-12-31"):
    out = {}
    for k, closes in dates_by_market.items():
        out[k] = {"close": closes, "first": first, "last": last}
    return out


def test_aperturas_sin_festivos_de_toda_la_ventana():
    d = date(2026, 10, 27)
    all_open = {k: {d.isoformat(): (1.0, 1.0)} for k in ("frankfurt", "londres", "madrid", "tokio", "seul", "shanghai",
                                                          "hongkong", "nuevayork", "saopaulo")}
    ops = bt.build_openings(d, d, _idx(all_open))
    assert [o["id"] for o in ops] == ["tokio-seul", "china", "europa", "saopaulo", "nuevayork"]
    london_off = {**all_open, "londres": {}}
    assert "europa" in [o["id"] for o in bt.build_openings(d, d, _idx(london_off))]     # Fráncfort abre
    eu_off = {**all_open, "londres": {}, "frankfurt": {}, "madrid": {}}
    assert "europa" not in [o["id"] for o in bt.build_openings(d, d, _idx(eu_off))]      # festivo en toda Europa
    assert bt.build_openings(date(2026, 10, 24), date(2026, 10, 25), _idx(all_open)) == []


def test_encadenamiento_bolsa_que_cerro_justo_antes():
    d = "2026-10-27"
    closes = {k: {d: (101.0, 100.0), "2026-10-26": (100.0, 99.0)} for k in bt.CHAIN_PREF}
    closes["shanghai"] = {d: (99.0, 100.0)}   # Shanghái cierra a las 07:00 UTC, antes que nadie antes de Europa
    eu = next(o for o in ap.openings_on(date(2026, 10, 27)) if o["id"] == "europa")
    sign, txt = bt.chain_signal(eu, _idx(closes))
    assert sign == -1 and "Shanghái" in txt
    ny = next(o for o in ap.openings_on(date(2026, 10, 27)) if o["id"] == "nuevayork")
    assert bt.chain_signal(ny, _idx(closes))[1].startswith("Hong Kong")   # Europa sigue abierta al abrir Nueva York
    tk = next(o for o in ap.openings_on(date(2026, 10, 27)) if o["id"] == "tokio-seul")
    assert bt.chain_signal(tk, _idx(closes))[1].startswith("Nueva York")  # la sesión de ayer de Nueva York
    assert bt.chain_signal(eu, _idx(closes, last="2026-10-26")) is None   # sin el dato de hoy: no se inventa


# ---------------------------------------------------------------- simulación

FEE, SLIP = 0.001, 0.0002


def test_trade_stop_dinamico_pesimista_y_costes():
    bars = [bar(T0, 100, 100.2, 99.9, 100.1)]
    bars += [bar(T0 + MIN, 100.1, 102.0, 100.1, 101.9), bar(T0 + 2 * MIN, 101.9, 101.95, 100.0, 100.2)]
    r = bt.simulate_trade(bars, 0, 1, 1.0, 0.5, T0 + 240 * MIN, FEE, SLIP)
    entry = 100 * 1.0002
    stop = 102.0 * 0.995                                        # trailing 0,5 % desde 102 (ya en +1 R)
    # Pesimista: el máximo de la vela 1 sube el stop a 101,49 y su mínimo (100,1) lo toca: sale ahí, aunque cierre más
    # arriba (seguir sería suponer que el mínimo llegó antes que el máximo).
    assert r["why"] == "dinamico" and r["k"] == 1
    ex = stop * (1 - SLIP)
    assert r["ret"] == pytest.approx(((ex / entry - 1) - FEE * (1 + ex / entry)) * 100)
    # Vela que abre por debajo del stop: sale en la apertura (peor que el stop).
    gap = [bar(T0, 100, 100.0, 99.95, 100), bar(T0 + MIN, 98.0, 98.2, 97.5, 98.0)]   # sin avance: stop inicial
    r = bt.simulate_trade(gap, 0, 1, 1.0, 0.5, T0 + 240 * MIN, FEE, SLIP)
    assert r["why"] == "inicial" and r["ret"] < -2.0
    # Corto que llega al fin de la ventana.
    tm = [bar(T0 + k * MIN, 100 - k * 0.01, 100 - k * 0.01 + 0.02, 100 - k * 0.01 - 0.02, 100 - k * 0.01) for k in range(5)]
    r = bt.simulate_trade(tm, 0, -1, 1.0, 2.0, T0 + 3 * MIN, FEE, SLIP)
    assert r["why"] == "tiempo" and r["k"] == 2


def test_estadisticas_por_apertura():
    recs = [(1, "europa", "BTC", 4.0, 1, 1), (2, "europa", "BTC", -1.0, 2, 0), (3, "europa", "BTC", 0.0, 0, 0),
            (4, "europa", "BTC", -2.0, 1, 0), (5, "europa", "BTC", 1.0, 1, 1)]
    s = bt.stats(recs)
    assert s["openings"] == 5 and s["trades"] == 5 and s["reentries"] == 1 and s["with_trades"] == 4
    assert s["mean_pct"] == pytest.approx(0.4) and s["median_pct"] == 0.0 and s["share_ge_3"] == pytest.approx(0.2)
    assert s["win_rate"] == pytest.approx(0.4) and s["max_dd_pct"] == pytest.approx(3.0)
    assert s["mean_traded_pct"] == pytest.approx(0.5) and s["share_ge_3_traded"] == pytest.approx(0.25)
    assert bt.stats([]) == {"openings": 0, "trades": 0}


# ---------------------------------------------------------------- v2: entrada, operación y registro

def flat(t0, n, px=100.0, v=10.0, tb=5.0):
    return [bar(t0 + k * MIN, px, px + 0.1, px - 0.1, px, v, tb) for k in range(n)]


def past_session(po, win, hold=4, pre=250):
    """pre min planos, rango de 5 min (99,5–100,5), ruptura al alza con volumen, `hold` velas por encima, la vela de
    entrada y luego el objetivo (win) o el stop; plana hasta las 4 h."""
    bs = flat(po - pre * MIN, pre)                          # 4 h previas: MOVING y ATR de 5 min
    bs += [bar(po + k * MIN, 100, 100.5, 99.5, 100) for k in range(5)]
    bs.append(bar(po + 5 * MIN, 100.4, 101.2, 100.0, 101.0, 60.0, 50.0))
    bs += [bar(po + (6 + k) * MIN, 101.0, 101.3, 100.9, 101.1) for k in range(hold)]
    t = po + (6 + hold) * MIN
    bs.append(bar(t, 101.1, 101.4, 101.0, 101.2))
    bs.append(bar(t + MIN, 101.2, 104.0, 101.1, 103.8) if win else bar(t + MIN, 101.1, 101.2, 99.0, 99.2))
    return bs + flat(t + 2 * MIN, 240 - (8 + hold), 103.8 if win else 99.2)


P5 = bt.make_combo("or_mid", None, "2R", 0.0, 1, range_min=5, rvol_min=1.5)["params"]


def _feats(bars, open_ms, p=P5):
    i0, i_end = ap.bar_index(bars, open_ms), ap.bar_index(bars, open_ms + 240 * MIN)
    f = [None] * len(bars)
    for i in range(i0, i_end):
        f[i] = ap.features(bars, i, open_ms, None, None, {**p, "stop_mode": "atr5"})
    return i0, i_end, f


def test_primera_entrada_igual_que_session_case_del_motor():
    """Lo simulado y lo vivo coinciden: find_entry + record da lo mismo que apertura.session_case (la puerta)."""
    for win in (True, False):
        bars = past_session(T0, win)
        i0, i_end, f = _feats(bars, T0)
        for d in (1, -1):
            e = bt.find_entry("SOL", T0, bars, i0, i_end, f, d, P5, SLIP)
            c = ap.session_case("SOL", d, T0, bars, None, [], P5, FEE, SLIP)
            if c is None:
                assert e is None and d == -1                    # a la baja no rompe
                continue
            rec, _ = bt.record(bars, e, P5, i_end, T0 + 240 * MIN, FEE, SLIP)
            assert bt.FIRST[rec[7]] == c["first"] and round(rec[4], 4) == c["R"] and rec[6] == c["tR"]
            assert rec[3] == pytest.approx(c["ret"], abs=1e-4) and e[2]["t"] == c["t"]
            assert c["first"] == ("objetivo" if win else "stop")
        free = bt.find_entry("SOL", T0, bars, i0, i_end, f, 0, P5, SLIP)   # ruptura libre: el mismo lado
        assert free[1] == 1 and free[0] == bt.find_entry("SOL", T0, bars, i0, i_end, f, 1, P5, SLIP)[0]


def test_otra_ventana_con_posicion_no_deja_entrar():
    bars = past_session(T0, True)
    i0, i_end, f = _feats(bars, T0)
    e = bt.find_entry("SOL", T0, bars, i0, i_end, f, 1, P5, SLIP)
    assert bt.find_entry("SOL", T0, bars, i0, i_end, f, 1, P5, SLIP, busy_until=bars[e[0]][0]) == e   # ya libre
    assert bt.find_entry("SOL", T0, bars, i0, i_end, f, 1, P5, SLIP, busy_until=T0 + 300 * MIN) is None


def test_simulate_plan_da_parcial_y_mae():
    bars = [bar(T0, 100, 101.1, 99.9, 101.0), bar(T0 + MIN, 101.0, 101.0, 98.8, 99.0)]
    r = ap.simulate_plan(bars, 0, 1, {"stopPct": 1.0, "tp1R": 1, "tp1Frac": 0.5, "tp2R": 3, "beR": None},
                         T0 + 60 * MIN, FEE, 0.0)
    assert r["partial"] and r["first"] == "stop"
    assert r["mae"] == pytest.approx(-1.0) and r["mfe"] == pytest.approx(1.1)   # MAE hasta el stop (99), no 98,8
    r = ap.simulate_plan(bars, 0, 1, {"stopPct": 1.0, "tp2R": 3, "beR": None}, T0 + 60 * MIN, FEE, 0.0)
    assert not r["partial"]
    gap = [bar(T0, 100, 100.2, 99.9, 100.1), bar(T0 + MIN, 98.5, 98.6, 98.0, 98.2)]   # abre por debajo del stop
    assert ap.simulate_plan(gap, 0, 1, {"stopPct": 1.0, "tp2R": 3, "beR": None}, T0 + 60 * MIN, FEE, 0.0)["mae"] == \
        pytest.approx(-1.5)
    for d in (1, -1):                                       # sale en el objetivo: MFE hasta el objetivo (+2 %)
        hit = [bar(T0, 100, 100.4 if d > 0 else 100.3, 99.7 if d > 0 else 99.6, 100.0),
               bar(T0 + MIN, 100.0, 103.0 if d > 0 else 100.1, 99.9 if d > 0 else 97.0, 100 + d * 2.5)]
        r = ap.simulate_plan(hit, 0, d, {"stopPct": 1.0, "tp2R": 2, "beR": None}, T0 + 60 * MIN, FEE, 0.0)
        assert r["first"] == "objetivo" and r["mfe"] == pytest.approx(2.0) and r["mae"] == pytest.approx(-0.3), d


def test_excursion_de_la_ventana_y_mas_3_antes_del_stop():
    up = [bar(T0, 100, 100.2, 99.8, 100), bar(T0 + MIN, 100, 103.5, 99.9, 103), bar(T0 + 2 * MIN, 103, 103, 98, 98.5)]
    mfe, mae, hit = bt.window_excursion(up, 0, 3, 1, 100.0, 1.0)
    assert hit == 1 and mfe == pytest.approx(3.5) and mae == pytest.approx(-2.0)
    first_stop = [bar(T0, 100, 100.2, 98.9, 99), bar(T0 + MIN, 99, 104, 99, 104)]
    mfe, mae, hit = bt.window_excursion(first_stop, 0, 2, 1, 100.0, 1.0)
    assert hit == 0 and mfe == pytest.approx(4.0)          # llegó a +4 % pero después del stop
    both = [bar(T0, 100, 103.5, 98.9, 100)]                # la misma vela toca los dos: cuenta el stop
    assert bt.window_excursion(both, 0, 1, 1, 100.0, 1.0)[2] == 0
    short = [bar(T0, 100, 100.2, 96.5, 97)]
    assert bt.window_excursion(short, 0, 1, -1, 100.0, 1.0) == (pytest.approx(3.5), pytest.approx(-0.2), 1)


def _sessions_file(tmp_path, wins):
    rows = []
    for k, w in enumerate(wins):
        rows += past_session(T0 + k * 86400_000, w)
    path = tmp_path / "SOLUSDT-1m-2026-10-api.json"
    path.write_text(json.dumps({"rows": rows}), encoding="utf-8")
    ops = [{"id": "europa", "name": "Europa", "markets": ["frankfurt", "londres", "madrid"], "lead": "londres",
            "open_ms": T0 + k * 86400_000} for k in range(len(wins))]
    return path, ops


def test_run_coin_puerta_en_avance_y_solo_con_sesiones_anteriores(tmp_path):
    path, ops = _sessions_file(tmp_path, [True, True, True, False])
    p = {**P5, "rvol_min": 0.5, "gate_min_n": 2, "gate_sessions": 10}   # sesiones iguales: rvol ≈ 1 desde la 3.ª
    chain = {f"europa|{o['open_ms']}": 1 for o in ops}
    r = bt.run_coin(("SOL", [str(path)], ops, chain, [p], ["europa"], FEE, SLIP, True, True))
    assert len(r["rows"]) == 4 and len(r["eff"]) == 4
    vi = bt.VKEYS.index("encadenamiento")
    off, on = bt.decode(r["out"][0][vi][0]), bt.decode(r["out"][0][vi][1])
    assert [int(x[bt.I_OPEN]) for x in off] == [o["open_ms"] for o in ops]
    assert [bt.WHYS[int(x[bt.I_WHY])] for x in off] == ["objetivo"] * 3 + ["inicial"]
    # Puerta en avance: las dos primeras sesiones no tienen 2 casos anteriores; la 3.ª y la 4.ª sí (todos ganan).
    assert [int(x[bt.I_OPEN]) for x in on] == [ops[2]["open_ms"], ops[3]["open_ms"]]
    assert [x[bt.I_GOK] for x in off] == [0.0, 0.0, 1.0, 1.0] and off[2][bt.I_GDEC] == 2
    assert bt.decode(r["out"][0][bt.VKEYS.index("oraculo")][0])[0][bt.I_DIR] == 1   # la 4.ª acaba abajo: sin ruptura
    assert len(bt.decode(r["out"][0][bt.VKEYS.index("oraculo")][0])) == 3
    x = off[0]
    assert x[bt.I_RR] == pytest.approx(1.94, abs=0.02) and x[bt.I_R] > 1.5 and x[bt.I_MFEW] > 2.5


# ---------------------------------------------------------------- v2: cuenta, estadística y selección

def row(open_ms, coin, entry, exit_, ret, stop=1.0, R=None, why="objetivo", wid=0, hit3=0, mfew=0.0, gok=-1.0):
    x = [0.0] * bt.NC
    x[bt.I_OPEN], x[bt.I_WID], x[bt.I_COIN], x[bt.I_DIR] = open_ms, wid, coin, 1
    x[bt.I_ENTRY], x[bt.I_EXIT], x[bt.I_RET], x[bt.I_STOP] = entry, exit_, ret, stop
    x[bt.I_R] = ret / stop if R is None else R
    x[bt.I_WHY], x[bt.I_HIT3], x[bt.I_MFEW], x[bt.I_GOK] = bt.WHY_CODE[why], hit3, mfew, gok
    return tuple(x)


DAY = bt.mad_day(T0)


def test_cuenta_diaria_con_exposicion_tope_de_entradas_y_apalancamiento():
    p = bt.BASE   # 3 entradas al día y 1,5 % de riesgo comprometido
    rows = [row(T0, 0, T0, T0 + 60 * MIN, 2.0), row(T0, 1, T0, T0 + 60 * MIN, -1.0),
            row(T0, 2, T0, T0 + 60 * MIN, 1.0),                       # 3.ª a la vez: 40 % + 40 % + 40 % > 100 %
            row(T0, 2, T0 + 90 * MIN, T0 + 120 * MIN, 1.0),           # ya cerradas las otras: entra (3.ª del día)
            row(T0, 3, T0 + 130 * MIN, T0 + 150 * MIN, 5.0)]          # 4.ª entrada: tope diario
    sim = bt.account_sim(rows, p, [DAY])
    assert sim["taken"] == [True, True, False, True, False]
    assert sim["skip"] == [None, None, "cuenta", None, "limite"]       # motivo contado de cada una que no entra
    day = sim["days"][0]
    assert day[2] == 3 and day[3] and day[4]                          # tope de entradas y exposición
    # Stop 1 %: 0,5 % de riesgo pediría el 50 % del capital; manda el tope del 40 % (4.000 USDC).
    eq1 = 10000 + 4000 * 0.02 - 4000 * 0.01
    assert day[1] == pytest.approx((eq1 + eq1 * 0.4 * 0.01 - 10000) / 100)
    lev = bt.account_sim(rows, p, [DAY], lev=2.0, take=sim["taken"])
    assert lev["taken"] == sim["taken"] and lev["days"][0][1] == pytest.approx(2 * day[1], rel=0.01)
    budget = bt.account_sim(rows[:2], {**p, "max_day_entries": None, "max_day_risk_pct": 0.5}, [DAY])
    assert budget["taken"] == [True, False] and budget["days"][0][3]  # 0,4 % comprometido + 0,5 % > 0,5 %


def test_estadisticas_por_operacion_y_por_dia():
    rows = [row(1, 0, 1, 2, 2.0, R=2.0, hit3=1, mfew=4.0), row(2, 0, 2, 3, -1.0, R=-1.0, why="inicial"),
            row(3, 0, 3, 4, -1.0, R=-1.0, why="inicial"), row(4, 0, 4, 5, 0.3, R=0.3, why="tiempo")]
    s = bt.trade_stats(rows)
    assert s["trades"] == 4 and s["mean_R"] == pytest.approx(0.075) and s["win_rate"] == 0.5
    assert s["exit"] == {"objetivo": 0.25, "inicial": 0.5, "tiempo": 0.25}
    assert s["mfe_ge_3_before_stop"] == 0.25 and s["mfe_ge_3_window"] == 0.25 and s["profit_factor"] == 1.15
    assert bt.trade_stats([]) == {"trades": 0}
    days = [("2026-01-0%d" % k, x, 1, k == 2, False) for k, x in enumerate([1.0, -2.0, 0.0, 3.5, 5.0], 1)]
    d = bt.day_stats(days)
    assert d["days"] == 5 and d["share_ge_1"] == 0.6 and d["share_ge_3"] == 0.4 and d["share_ge_5"] == 0.2
    assert d["worst_pct"] == -2.0 and d["limit_days"] == 1 and d["median_pct"] == 1.0
    assert d["max_dd_pct"] == pytest.approx(2.0)
    assert d["total_pct"] == pytest.approx((1.01 * .98 * 1.035 * 1.05 - 1) * 100, abs=1e-3)
    sp = bt.split_days(days, "2026-01-04")
    assert sp["is"]["days"] == 3 and sp["oos"]["days"] == 2


def test_seleccion_solo_con_la_muestra():
    split = 100
    good_is = [row(k, 0, k, k + 1, 1.0) for k in range(150)]           # gana dentro, pierde fuera
    bad_is = [row(k, 0, k, k + 1, -0.2) for k in range(150)]           # pierde dentro, gana fuera
    a = [r if r[bt.I_OPEN] < split else row(r[0], 0, r[4], r[5], -3.0) for r in good_is]
    b = [r if r[bt.I_OPEN] < split else row(r[0], 0, r[4], r[5], 9.0) for r in bad_is]
    st = {k: {"is": bt.trade_stats([r for r in x if r[0] < split]), "oos": bt.trade_stats([r for r in x if r[0] >= split])}
          for k, x in ((0, a), (1, b))}
    assert bt.choose([(k, v["is"]) for k, v in st.items()]) == 0
    assert st[0]["oos"]["mean_R"] < 0 < st[1]["oos"]["mean_R"]       # el resultado fuera no cambia la elección
    few = {"trades": 10, "mean_R": 5.0}
    assert bt.choose([(0, st[0]["is"]), (1, few)]) == 0                # con pocas operaciones no se puede elegir
    assert bt.choose([(1, few)]) == 1


def test_rejilla_de_salidas_y_textos():
    combos = bt.exit_combos()
    assert len(combos) == 144 and len({bt.entry_key(c["params"]) for c in combos}) == 24
    c = bt.make_combo("or_far", 1.5, "trail", 0.5, 5)
    p = c["params"]
    assert p["trail_atr"] == 3.0 and p["trail_tf"] == 5 and p["tp1_r"] == 2.0 and p["target_r"] is None
    assert p["max_reentries"] == 0 and p["moving_pct"] == 3.0 and p["max_day_entries"] == 3 and not p["gate"]
    assert bt.combo_text(c["label"]) == ("stop en el otro lado del rango · sin tope, con stop dinámico de 3 ATR de 5 min "
                                         "· break-even (cubriendo costes) a +1,5 R · mitad en +2 R · confirmación al "
                                         "cierre de 5 min · rango 30 min · rvol ≥ 2,0")
    plan = ap.entry_plan(1, 101.0, {"stop_px": 100.0, "target_px": None, "trailPct": 0.9}, p)
    assert plan["beR"] == 1.5 and plan["tp1Frac"] == 0.5 and plan["tp1R"] == 2.0 and plan["tp2R"] is None


def test_referencia_es_el_preset_v2_y_motivos_de_la_puerta():
    ref = bt.make_combo(**bt.REF_V2)["params"]
    assert ref == {**ap.PARAMS, **ap.V2, "gate": False}           # el preset V2 de apertura.py tal cual, sin puerta
    p = {"gate_min_n": 20, "gate_pp": 5.0}
    rows = [row(1, 0, 1, 2, 1.0, gok=1.0), row(2, 0, 2, 3, -1.0, gok=0.0),
            row(3, 0, 3, 4, -1.0, gok=0.0), row(4, 0, 4, 5, -1.0, gok=0.0), row(500, 0, 500, 501, 2.0, gok=1.0)]
    rows[1] = rows[1][:bt.I_GEDGE] + (math.nan, 3.0)               # 3 casos decididos: pocos
    rows[2] = rows[2][:bt.I_GEDGE] + (1.0, 25.0)                   # 25 casos, +1 punto: sin ventaja
    rows[3] = rows[3][:bt.I_GEDGE] + (9.0, 25.0)                   # ventaja, pero R medio pasado ≤ 0
    g = bt.gate_split(rows, 100, p)
    assert g["is"]["reasons"] == {"ok": 0.25, "pocos_casos": 0.25, "sin_ventaja": 0.25, "r_medio_no_positivo": 0.25}
    assert g["is"]["pass"]["trades"] == 1 and g["is"]["fail"]["mean_R"] == -1.0 and g["oos"]["reasons"]["ok"] == 1.0


def test_recomendado_lo_carga_el_motor(tmp_path, monkeypatch):
    monkeypatch.setattr(bt.config, "BACKTEST_DIR", tmp_path)
    p = bt.make_combo("or_far", None, "3R", 0.0, 5, range_min=15, rvol_min=1.5)["params"]
    rec = {**{k: p[k] for k in bt.REC_KEYS}, "gate": True}
    path = tmp_path / "aperturas.json"
    path.write_text(json.dumps({"version": 2, "period": {"months": 24}, "recommended": rec}), encoding="utf-8")
    eng = ap.OpeningEngine(None, None, lambda c, w: {}, lambda *a: True, lambda: {})
    for k in ("range_min", "rvol_min", "stop_mode", "target_r", "confirm_tf", "be_r", "trail_atr", "max_reentries",
              "moving_pct", "max_day_entries", "max_day_risk_pct", "gate", "max_cost_r"):
        assert eng.p[k] == rec[k], k
    assert eng.p["be_r"] is None and eng.p["gate"] is True and eng._lag_ms == ap.LIVE_LAG_MS + 4 * MIN
    assert eng.source == "backtest" and eng.notice is None


def test_sin_recomendacion_valida_preset_v2_con_aviso_nunca_la_v1(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(bt.config, "BACKTEST_DIR", tmp_path)
    path = tmp_path / "aperturas.json"
    rec = {"stop_mode": "atr5", "range_min": 15}
    for body, why in ((None, "falta aperturas.json"),
                      ({"version": 2, "period": {"months": 3}, "recommended": rec}, "sale de 3 meses (< 24)"),
                      ({"period": {"months": 24}, "recommended": {"range_min": 30, "trail_atr": 10.0}}, "de la v1"),
                      ("{roto", "No pude leer")):
        if body is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
        assert ap.load_recommendation()[0] == {}
        eng = ap.OpeningEngine(None, None, lambda c, w: {}, lambda *a: True, lambda: {})
        assert eng.p == {**ap.PARAMS, **ap.V2} and not eng.p["gate"] and eng.p["stop_mode"] == "or_mid", why
        assert eng.source == "preset_v2" and why in eng.notice and "no la v1" in eng.notice
        st = eng.build_status(T0)
        assert st["params_source"] == "preset_v2" and st["notice"] == eng.notice
    assert "preset V2" in caplog.text
    manual = ap.OpeningEngine(None, None, lambda c, w: {}, lambda *a: True, lambda: {}, params={"range_min": 5})
    assert manual.source == "manual" and manual.p["stop_mode"] == "cercano" and manual.notice is None


def test_pasada_corta_o_sin_red_no_pisa_la_recomendacion():
    assert bt.out_name(24, False) == "aperturas.json" and bt.out_name(36, False) == "aperturas.json"
    assert bt.out_name(3, False) == "aperturas-3m.json" and bt.out_name(24, True) == "aperturas-24m-offline.json"
    assert bt.out_name(3, True, "../../C/prueba.json") == "prueba.json"      # siempre dentro de BACKTEST_DIR


def _fake_report(mean_r=-0.1, t=-3.0, ny=20):
    ts = {"trades": 300, "win_rate": 0.4, "mean_R": mean_r, "median_R": -1.0, "t_stat": t, "mean_ret_pct": -0.1,
          "exit": {"inicial": 0.6, "objetivo": 0.4}, "mfe_ge_3_before_stop": 0.05, "share_ret_ge_3": 0.02,
          "mfe_ge_3_window": 0.1}
    op = {"openings": 1000, "trades": 300, "mean_pct": -0.03, "share_ge_3": 0.006}
    var = lambda v: {"name": bt.VARIANTS[v]["name"], "chosen_text": "stop en el medio del rango · objetivo 2R",
                     "chosen": {"gate": False}, "trades_is": ts, "trades_oos": ts, "oos": op,
                     "by_window": [{"window": "nuevayork", "trades_oos": {**ts, "trades": ny}},
                                   {"window": "europa", "trades_oos": ts}],
                     "windows_oos": [{"window": "nuevayork", "found": ny + 10, "taken": ny, "busy": 80, "day_limit": 6,
                                      "account": 4},
                                     {"window": "europa", "found": 320, "taken": 300, "busy": 0, "day_limit": 15,
                                      "account": 5}]}
    dd = {"days": 150, "median_pct": 0.0, "mean_pct": -0.02, "p5_pct": -0.6, "p95_pct": 0.5, "worst_pct": -1.2,
          "share_ge_1": 0.03, "share_ge_3": 0.0, "share_ge_5": 0.0, "total_pct": -3.0, "max_dd_pct": 4.0}
    reasons = {"ok": 0.0, "pocos_casos": 0.7, "sin_ventaja": 0.1, "r_medio_no_positivo": 0.2}
    g = lambda con: {"oos": {"con": con, "sin": ts}, "daily_oos": {"con": {"days": 0}, "sin": dd},
                     "by_prediction": {"oos": {"reasons": reasons}}}
    return {"variants": {v: var(v) for v in bt.VKEYS}, "period": {"from": "a", "to": "b", "split": "c"},
            "openings": {"total": 2614},
            "daily": {"encadenamiento": {"lev1": {"oos": dd}, "lev2": {"oos": {**dd, "mean_pct": -0.04, "worst_pct": -2.4}}}},
            "gate": {"encadenamiento": {"chosen": False, "sessions": None,
                                        "options": {"60": g({"trades": 0}), "250": g({"trades": 0})},
                                        "reference": {"text": "preset", "options": {"60": g({"trades": 0}),
                                                                                    "250": g({**ts, "trades": 40})}}}},
            "exits": {"focus": {"encadenamiento": {"text": "x", "oos": ts, "daily": {"oos": dd}}}}, "opening_effect": [],
            "v1_previous": {"oos": {"encadenamiento": {"mean_pct": -0.06, "mean_trade_pct": -0.22}}}}


def test_veredicto_honesto_y_resumen_en_espanol():
    res = _fake_report()
    v = "\n".join(bt.verdict(res))
    assert "pierde con costes: no hay ventaja" in v and "TECHO INALCANZABLE" in v
    assert "amplifica las pérdidas igual que las ganancias" in v and "-0,060 % por apertura y -0,220 % por operación" in v
    assert "no se activa" in v and "Solo Nueva York en SOL/ETH/XRP" in v and "con 60 sesiones de historia no deja" in v
    assert "pocos casos decididos en el 70,0 %" in v and "preset V2 del motor tal cual (preset)" in v
    assert "con 250 sesiones, R medio -0,100 en 40 operaciones" in v and "no crea ventaja" not in v
    assert ("Nueva York opera poco con la configuración global: 20 de 320 operaciones fuera de muestra (6,2 %). De "
            "sus 110 rupturas con entrada fuera de muestra se tomaron 20; 80 se perdieron porque otra ventana ocupaba "
            "la moneda, 6 por el tope de entradas o el presupuesto de riesgo del día") in v
    assert "Nueva York opera poco" not in " ".join(bt.verdict(_fake_report(ny=150)))   # 33 %: no es poco
    pos = "\n".join(bt.verdict(_fake_report(mean_r=0.05, t=1.2)))
    assert "indistinguible del azar" in pos and "ventaja" in pos       # positivo sin t ≥ 2: no es ventaja
    res["verdict"] = []
    txt = "\n".join(bt.summary_text(res))
    assert "R medio -0,100" in txt and "Cuenta Encadenamiento 2:1" in txt


def test_json_sin_nan():
    out = bt._clean({"a": float("nan"), "b": [1.0, float("inf"), (2, float("-inf"))], "c": "x"})
    assert out == {"a": None, "b": [1.0, None, [2, None]], "c": "x"}
    json.dumps(out, allow_nan=False)


# ---------------------------------------------------------------- revisión: ventanas solapadas, corte y motivos

def _overlap_file(tmp_path):
    """São Paulo (13:00 UTC, rango ancho 98,7–101,3) y Nueva York (13:30, rango 99,5–100,5) en SOL: Nueva York rompe
    a las 13:35 y São Paulo a las 13:38, con la posición de Nueva York abierta hasta el fin de su sesión."""
    sp = T0 + 5 * 3600_000
    ny = sp + 30 * MIN
    bs = flat(sp - 250 * MIN, 250)
    bs += [bar(sp + k * MIN, 100, 101.3, 98.7, 100) for k in range(5)] + flat(sp + 5 * MIN, 25)
    bs += [bar(ny + k * MIN, 100, 100.5, 99.5, 100) for k in range(5)]
    bs.append(bar(ny + 5 * MIN, 100.4, 101.2, 100.0, 101.0, 60.0, 50.0))       # ruptura de Nueva York
    bs += [bar(ny + k * MIN, 101.0, 101.2, 100.9, 101.1) for k in (6, 7)]
    bs.append(bar(ny + 8 * MIN, 101.1, 101.7, 101.0, 101.6, 60.0, 50.0))       # ruptura de São Paulo
    bs += [bar(ny + k * MIN, 101.6, 101.7, 101.5, 101.6) for k in range(9, 242)]
    path = tmp_path / "SOLUSDT-1m-2026-10-api.json"
    path.write_text(json.dumps({"rows": bs}), encoding="utf-8")
    ops = [{"id": "saopaulo", "lead": "saopaulo", "open_ms": sp}, {"id": "nuevayork", "lead": "nuevayork", "open_ms": ny}]
    return path, ops, sp, ny


def test_ventanas_solapadas_por_orden_de_entrada_como_en_vivo(tmp_path):
    """Entra la primera ruptura (Nueva York, aunque abra después) y São Paulo, que rompe con la posición abierta, la
    pierde; antes el backtest se la daba a São Paulo por abrir antes."""
    path, ops, sp, ny = _overlap_file(tmp_path)
    chain = {f"{o['id']}|{o['open_ms']}": 1 for o in ops}
    r = bt.run_coin(("SOL", [str(path)], ops, chain, [P5], ["saopaulo", "nuevayork"], FEE, SLIP, False, False))
    for v in ("libre", "encadenamiento"):
        vi = bt.VKEYS.index(v)
        rows = bt.decode(r["out"][0][vi][0])
        assert [(int(x[bt.I_WID]), int(x[bt.I_ENTRY])) for x in rows] == [(1, ny + 6 * MIN)], v
        assert rows[0][bt.I_EXIT] == ny + 240 * MIN and bt.WHYS[int(rows[0][bt.I_WHY])] == "tiempo"
        assert list(r["lost"][0][vi][0]) == [sp, 0.0], v                # São Paulo: perdida por la moneda ocupada
    alone = bt.run_coin(("SOL", [str(path)], ops[:1], chain, [P5], ["saopaulo", "nuevayork"], FEE, SLIP, False, False))
    vi = bt.VKEYS.index("encadenamiento")
    assert [int(x[bt.I_ENTRY]) for x in bt.decode(alone["out"][0][vi][0])] == [ny + 9 * MIN]   # sola sí entraba


def test_corte_en_la_medianoche_de_madrid():
    times = [T0 + k * 6 * 3600_000 for k in range(10)]                # el 70 %: 28-10 a las 03:00 de Madrid
    ms, day = bt.split_point(times)
    assert day == "2026-10-29" and ms > times[7] and bt.mad_day(ms - 1) == "2026-10-28"
    assert datetime.fromtimestamp(ms / 1000, bt.MAD).strftime("%H:%M") == "00:00"
    mid = int(datetime(2026, 10, 28, 23, 0, tzinfo=timezone.utc).timestamp() * 1000)   # ya es medianoche en Madrid
    assert bt.split_point(times[:7] + [mid, mid + 1, mid + 2]) == (mid, "2026-10-29")


def test_motivos_por_ventana_fuera_de_muestra():
    rows = [row(T0, 0, T0, T0 + 60 * MIN, 2.0), row(T0, 1, T0, T0 + 60 * MIN, -1.0), row(T0, 2, T0, T0 + 60 * MIN, 1.0),
            row(T0, 2, T0 + 90 * MIN, T0 + 120 * MIN, 1.0), row(T0, 3, T0 + 130 * MIN, T0 + 150 * MIN, 5.0, wid=1),
            row(1, 3, 1, 2, 1.0, wid=1)]                                   # dentro de muestra: no cuenta
    sim = bt.account_sim(rows, bt.BASE, [DAY, bt.mad_day(1)])
    lost = [[[array.array("d", [T0, 1, 1, 1])]]]                         # una fuera (T0) y otra dentro (1)
    out = bt.window_blocks([{"lost": lost}], 0, 0, 0, {"rows": rows, "sim": sim}, ["europa", "nuevayork"], T0 - 1)
    assert out == [{"window": "europa", "found": 4, "taken": 3, "busy": 0, "day_limit": 0, "account": 1},
                   {"window": "nuevayork", "found": 1, "taken": 0, "busy": 1, "day_limit": 1, "account": 0}]


def test_el_backtest_no_mira_el_perfil_de_comisiones_del_bot(monkeypatch):
    """El backtest cobra FEE por lado (config.FEE_PCT) y con esa misma comisión van la puerta R:R, el suelo del stop, el
    objetivo y la regla del 3 % neto (comisiones.costes_de_backtest): con el perfil sin X-Perps elegido en el bot
    (0,74 % de ida y vuelta), session_case da lo mismo que con el de siempre, y run_coin corre con los costes del
    backtest. Fuera del bloque, el bot sigue con su perfil."""
    from bot import comisiones, paper
    p = {**bt.make_combo("or_mid", None, "3pct", 0.0, 1, range_min=5, rvol_min=1.5)["params"], "max_cost_r": 0.25}
    bars = past_session(T0, True)
    base = ap.session_case("SOL", 1, T0, bars, None, [], p, FEE, SLIP)
    assert base is not None
    comisiones.elegir(comisiones.SIN_XPERPS)
    assert ap.session_case("SOL", 1, T0, bars, None, [], p, FEE, SLIP) == base
    assert ap._session_case("SOL", 1, T0, bars, None, [], p, FEE, SLIP) != base   # sin el bloque, mezclaría costes
    assert comisiones.activo() == comisiones.SIN_XPERPS and paper.cost_rt_pct() == pytest.approx(0.74)
    with comisiones.costes_de_backtest(FEE * 100):
        assert comisiones.activo() == comisiones.BACKTEST and comisiones.hay_cortos()
        assert paper.cost_rt_pct() == pytest.approx(bt.COST_RT_PCT) == pytest.approx(paper.cost_rt_pct("perp"))
        assert ap.stop_floor_pct(p) == pytest.approx(bt.COST_RT_PCT / 0.25)
    assert comisiones.activo() == comisiones.SIN_XPERPS
    seen = []
    monkeypatch.setattr(bt, "_run_coin", lambda args: seen.append((comisiones.activo(), paper.cost_rt_pct())) or {})
    assert bt.run_coin(("SOL", [], [], None, [], [], FEE, SLIP, False, False)) == {}
    assert seen == [(comisiones.BACKTEST, pytest.approx(bt.COST_RT_PCT))]
