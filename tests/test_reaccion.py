"""bot/reaccion.py: la cripto en cada apertura de bolsa (contrato 2.14, «cripto_aperturas»). Horarios con cambio de hora y
festivos, estados por_abrir, en_curso y terminada, medida en vivo, historia y encadenamiento sin mirar el futuro, caché
de la historia en BACKTEST_DIR y robustez. Sin red: velas sintéticas."""
import asyncio
import json
import math
from datetime import date, datetime, timezone
from types import SimpleNamespace

import numpy as np
import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot import reaccion as R
from bot import regions
from bot.bus import Bus
from bot.server import Services, make_app
from bot.store import Store

UTC = timezone.utc
MIN, H, DAY = R.MIN, R.H, R.DAY
COINS = R.COINS


def ms(*a) -> int:
    return int(datetime(*a, tzinfo=UTC).timestamp() * 1000)


def bars(t0: int, t1: int, price=lambda t: 100.0) -> np.ndarray:
    """Velas de 1 min [t, o, h, l, c] de t0 a t1 con la apertura de cada vela en price(t) y el cierre en price(t + 1 min)."""
    ts = np.arange(t0, t1, MIN, dtype=float)
    o = np.array([price(t) for t in ts])
    c = np.array([price(t + MIN) for t in ts])
    return np.column_stack([ts, o, np.maximum(o, c), np.minimum(o, c), c])


# ---------------------------------------------------------------- horarios

def test_horarios_con_cambio_de_hora():
    # Europa pasó a invierno el 25-10-2026: Londres, Fráncfort y Madrid abren a las 07:00 UTC el viernes y a las 08:00
    # el lunes. EE. UU. cambia el 1-11: Nueva York abre a las 13:30 UTC el viernes 30 y a las 14:30 el lunes 2.
    for k in ("londres", "frankfurt", "madrid"):
        assert R.open_ms_on(k, date(2026, 10, 23)) == ms(2026, 10, 23, 7, 0)
        assert R.open_ms_on(k, date(2026, 10, 26)) == ms(2026, 10, 26, 8, 0)
    assert R.open_ms_on("nuevayork", date(2026, 10, 30)) == ms(2026, 10, 30, 13, 30)
    assert R.open_ms_on("nuevayork", date(2026, 11, 2)) == ms(2026, 11, 2, 14, 30)
    assert R.open_ms_on("tokio", date(2026, 10, 30)) == ms(2026, 10, 30, 0, 0)   # Japón no cambia de hora
    assert R.open_ms_on("saopaulo", date(2026, 10, 30)) == ms(2026, 10, 30, 13, 0)
    # Miércoles de ceniza en São Paulo: abre a las 13:00 locales (16:00 UTC)
    assert R.especial("saopaulo", date(2027, 2, 10)) and not R.especial("nuevayork", date(2027, 2, 10))
    assert R.open_ms_on("saopaulo", date(2027, 2, 10)) == ms(2027, 2, 10, 16, 0)
    # Ventanas del motor; Dubái no es una
    assert [R.ventana_de(k, date(2026, 10, 26)) for k in ("tokio", "seul", "shanghai", "hongkong", "frankfurt", "londres",
                                                         "madrid", "nuevayork", "saopaulo", "dubai")] == [
        "tokio-seul", "tokio-seul", "china", "china", "europa", "europa", "europa", "nuevayork", "saopaulo", None]


def test_festivos_y_dias_sin_sesion():
    assert not R.sesion("tokio", date(2026, 11, 3))          # Día de la Cultura
    assert not R.sesion("nuevayork", date(2026, 11, 26))     # Acción de Gracias
    assert not R.sesion("saopaulo", date(2026, 11, 2))       # Finados
    assert not R.sesion("londres", date(2026, 10, 31))       # sábado
    assert R.sesion("tokio", date(2026, 11, 4)) and R.sesion("dubai", date(2026, 11, 3))
    # Con los índices diarios (backtest_aperturas.index_data), los festivos que delata la historia
    idx = {"tokio": {"close": {"2024-01-04": (1, 1)}, "first": "2024-01-01", "last": "2024-12-31"}}
    assert R.sesion("tokio", date(2024, 1, 4), idx) and not R.sesion("tokio", date(2024, 1, 5), idx)
    assert R.sesion("tokio", date(2024, 1, 5))               # sin idx, cualquier laborable


@pytest.mark.parametrize("key,now,state,open_ms,next_ms", [
    # en curso: de 0 a 4 h desde la apertura
    ("londres", ms(2026, 10, 26, 9, 30), "en_curso", ms(2026, 10, 26, 8, 0), ms(2026, 10, 27, 8, 0)),
    ("londres", ms(2026, 10, 26, 8, 0), "en_curso", ms(2026, 10, 26, 8, 0), ms(2026, 10, 27, 8, 0)),
    # terminada: pasadas 4 h y hasta la siguiente apertura (también la noche anterior, en día de sesión)
    ("londres", ms(2026, 10, 26, 12, 0), "terminada", ms(2026, 10, 26, 8, 0), ms(2026, 10, 27, 8, 0)),
    ("londres", ms(2026, 10, 27, 7, 30), "terminada", ms(2026, 10, 26, 8, 0), ms(2026, 10, 27, 8, 0)),
    ("tokio", ms(2026, 10, 27, 23, 30), "terminada", ms(2026, 10, 27, 0, 0), ms(2026, 10, 28, 0, 0)),
    # por abrir: el lunes antes de Tokio (la última, el viernes, tiene más de 24 h)
    ("tokio", ms(2026, 11, 1, 23, 0), "por_abrir", ms(2026, 10, 30, 0, 0), ms(2026, 11, 2, 0, 0)),
    # por abrir: sábado en Nueva York aunque la última tenga menos de 24 h; la siguiente ya en horario de invierno
    ("nuevayork", ms(2026, 10, 31, 12, 0), "por_abrir", ms(2026, 10, 30, 13, 30), ms(2026, 11, 2, 14, 30)),
    # por abrir: Acción de Gracias (día sin sesión) con la del miércoles a 15,5 h
    ("nuevayork", ms(2026, 11, 26, 6, 0), "por_abrir", ms(2026, 11, 25, 14, 30), ms(2026, 11, 27, 14, 30)),
    # festivo en Tokio: el martes 3 no abre y la siguiente es el miércoles
    ("tokio", ms(2026, 11, 3, 1, 0), "por_abrir", ms(2026, 11, 2, 0, 0), ms(2026, 11, 4, 0, 0)),
])
def test_estados_de_la_sesion(key, now, state, open_ms, next_ms):
    past, nxt = R.sesiones(key, now)
    assert past[0] == open_ms and nxt == next_ms
    assert R.estado_sesion(key, now, past[0]) == state
    m = R.mercado(key, now, {})
    assert (m["state"], m["open_ms"], m["next_open_ms"]) == (state, open_ms, next_ms)
    assert m["minutes_since_open"] == ((now - open_ms) // MIN if state != "por_abrir" else None)
    assert m["coins"] is None and m["last"] is not None and m["last"]["coins"] is None   # sin velas


# ---------------------------------------------------------------- medida en vivo

def test_medida_desde_la_apertura():
    t0 = ms(2026, 10, 26, 8, 0)
    path = lambda t: 100.0 * (1 + 0.01 * math.sin((t - t0) / (60 * MIN) * math.pi))   # sube 1 % y vuelve
    a = bars(t0 - 30 * MIN, t0 + 5 * H, path)
    m = R.medida(a, t0, t0 + 4 * H)
    assert m["high_pct"] == pytest.approx(1.0, abs=0.01) and m["low_pct"] == pytest.approx(-1.0, abs=0.01)
    assert m["range_pct"] == pytest.approx(2.0, abs=0.02)
    assert m["ret_pct"] == pytest.approx(0.0, abs=0.01)        # a las 4 h (2 ciclos) vuelve a la apertura
    sp = m["spark"]
    assert len(sp) <= 60 and sp[0] == [t0, 0.0] and sp[-1][0] == t0 + 4 * H   # la última, al cierre de la última vela
    assert all(sp[k][0] < sp[k + 1][0] for k in range(len(sp) - 1))
    # Con las velas hasta ahora (en curso) solo cuenta lo que ya pasó
    now = t0 + 30 * MIN
    m2 = R.medida(a, t0, now + 1)
    assert m2["ret_pct"] == pytest.approx(path(now) / 100 * 100 - 100, abs=0.01) and m2["spark"][-1][0] == now + MIN
    assert "spark" not in R.medida(a, t0, t0 + 4 * H, spark=False)
    # Sin velas, o si la primera abre más de 5 min tarde, no hay medida
    assert R.medida(None, t0, t0 + H) is None and R.medida(np.empty((0, 5)), t0, t0 + H) is None
    assert R.medida(bars(t0 + 6 * MIN, t0 + H), t0, t0 + H) is None
    assert R.medida(bars(t0 + 5 * MIN, t0 + H), t0, t0 + H) is not None


def test_resultado_a_las_4_h_solo_con_las_velas_completas():
    """Hallazgo n.º 9: «terminada» y «last» enseñan el resultado a las 4 h solo si las velas llegan hasta 5 min antes de
    las 4 h (como la historia); con un hueco (reinicio, o la caché por detrás), la moneda no sale y «aviso» lo explica."""
    now = ms(2026, 10, 26, 13, 0)                       # Londres abrió a las 08:00: terminada a las 12:00
    t0 = ms(2026, 10, 26, 8, 0)
    full = {c: bars(ms(2026, 10, 20), now) for c in COINS}
    m = R.mercado("londres", now, full)
    assert m["state"] == "terminada" and set(m["coins"]) == set(COINS) and "aviso" not in m
    assert R.medida(full["BTC"], t0, t0 + 4 * H, completa=True) is not None
    # BTC se queda 13 min antes de las 4 h (y antes de la anterior, la del viernes, también): no sale a medias
    cut = {c: (a[(a[:, 0] < t0 + 4 * H - 13 * MIN) | (a[:, 0] >= t0 + 4 * H)] if c == "BTC" else a) for c, a in full.items()}
    m = R.mercado("londres", now, cut)
    assert m["state"] == "terminada" and "BTC" not in m["coins"] and set(m["coins"]) == {"ETH", "SOL", "XRP"}
    assert "Faltan velas de BTC" in m["aviso"] and "a medias" in m["aviso"]
    assert R.medida(cut["BTC"], t0, t0 + 4 * H, completa=True) is None and R.medida(cut["BTC"], t0, t0 + 4 * H) is not None
    # 4 min antes sí vale (tolerancia de 5 min, como la historia)
    ok = {c: a[a[:, 0] < t0 + 4 * H - 4 * MIN] if c == "BTC" else a for c, a in full.items()}
    assert "BTC" in R.mercado("londres", now, ok)["coins"]
    # La anterior completa («last») con hueco: sin esa moneda y con su aviso
    prev = ms(2026, 10, 23, 7, 0)
    gap = {c: a[(a[:, 0] < prev + 2 * H) | (a[:, 0] >= prev + 5 * H)] if c == "ETH" else a for c, a in full.items()}
    m = R.mercado("londres", now, gap)
    assert m["last"]["open_ms"] == prev and "ETH" not in m["last"]["coins"] and "apertura anterior" in m["aviso"]
    json.dumps(m)


def test_mensaje_con_velas_y_contrato():
    """Lunes 26-10-2026 a las 09:00 UTC: Europa en curso desde las 08:00 (con la anterior completa del viernes a las
    07:00, aún en horario de verano), Tokio y China terminadas, Nueva York y São Paulo por abrir (la del viernes tiene
    más de 24 h) y Dubái en curso desde las 06:00 UTC."""
    now = ms(2026, 10, 26, 9, 0)
    rise = lambda t: 100.0 * (1 + 0.001 * ((t - ms(2026, 10, 20)) / H))   # +0,1 puntos por hora
    pct = lambda a, b: (rise(b) / rise(a) - 1) * 100
    b = {c: bars(ms(2026, 10, 20), now, rise) for c in COINS}
    msg = R.mensaje(now, b)
    assert msg["type"] == "cripto_aperturas" and msg["ts"] == now and msg["coins"] == COINS
    assert list(msg["markets"]) == list(regions.MARKETS) and msg["chain"] is None
    json.dumps(msg)
    lo = msg["markets"]["londres"]
    assert (lo["name"], lo["window"], lo["state"], lo["minutes_since_open"]) == ("Londres", "europa", "en_curso", 60)
    btc = lo["coins"]["BTC"]
    assert btc["ret_pct"] == pytest.approx(pct(ms(2026, 10, 26, 8, 0), now), abs=1e-3)   # lo que lleva desde las 08:00
    assert btc["low_pct"] == pytest.approx(0.0, abs=1e-6) and len(btc["spark"]) <= 60
    assert lo["last"]["open_ms"] == ms(2026, 10, 23, 7, 0)
    assert lo["last"]["coins"]["ETH"]["ret_4h_pct"] == pytest.approx(pct(ms(2026, 10, 23, 7, 0), ms(2026, 10, 23, 11, 0)),
                                                                      abs=1e-3)
    assert set(lo["last"]["coins"]["ETH"]) == {"ret_4h_pct", "range_4h_pct"}
    tk = msg["markets"]["tokio"]
    assert tk["state"] == "terminada" and tk["open_ms"] == ms(2026, 10, 26, 0, 0)
    assert tk["coins"]["SOL"]["ret_pct"] == pytest.approx(pct(ms(2026, 10, 26), ms(2026, 10, 26, 4, 0)), abs=1e-3)   # 4 h
    assert tk["coins"]["SOL"]["spark"][-1][0] == ms(2026, 10, 26, 4, 0)
    assert msg["markets"]["nuevayork"]["state"] == "por_abrir" and msg["markets"]["nuevayork"]["coins"] is None
    assert msg["markets"]["nuevayork"]["last"]["open_ms"] == ms(2026, 10, 23, 13, 30)
    du = msg["markets"]["dubai"]
    assert du["window"] is None and du["state"] == "en_curso" and du["open_ms"] == ms(2026, 10, 26, 6, 0)
    assert msg["markets"]["shanghai"]["state"] == "terminada" and msg["markets"]["hongkong"]["window"] == "china"
    # Una moneda sin velas no sale; las demás sí
    b2 = {**b, "XRP": None}
    assert "XRP" not in R.mensaje(now, b2)["markets"]["londres"]["coins"]


# ---------------------------------------------------------------- historia y encadenamiento

def test_historia_sin_mirar_el_futuro_y_con_cobertura():
    """El rendimiento y el rango de una apertura solo usan las velas de [apertura, apertura + 4 h) (o 8 h): un salto
    de antes o de después no cuenta. Como los scripts de referencia: el rango pide 50 velas por hora y el rendimiento,
    primera y última vela a menos de 5 min de los extremos."""
    t0 = ms(2026, 10, 26, 8, 0)

    def price(t):
        if t < t0:
            return 90.0                                    # antes de abrir, otro nivel (no cuenta)
        if t <= t0 + 4 * H:
            return 100.0 * (1 + 0.01 * (t - t0) / (4 * H))  # +1 % en las 4 h
        return 120.0                                       # después de las 4 h, un salto del 20 %

    a = bars(t0 - 2 * H, t0 + 10 * H, price)
    s = (a[:, 0].astype(np.int64), a[:, 1], a[:, 2], a[:, 3], a[:, 4])
    opens = np.array([t0], np.int64)
    m = R.metricas(s, opens, opens, opens + 2 * H)
    assert m["ret"][0] == pytest.approx(0.01, abs=1e-6)
    assert m["r4"][0] == pytest.approx(1.0, abs=1e-6)       # 1 %: el salto de después no entra
    assert m["r8"][0] == pytest.approx(20.0, abs=1e-6)      # a 8 h sí
    assert m["extra"][0] == pytest.approx(0.005, abs=1e-6)  # lo que llevaba a las 2 h
    # Cobertura: con un hueco grande no hay rango, y si la primera vela llega tarde no hay rendimiento
    hole = a[(a[:, 0] < t0 + 30 * MIN) | (a[:, 0] >= t0 + 3 * H)]
    sh = (hole[:, 0].astype(np.int64), hole[:, 1], hole[:, 2], hole[:, 3], hole[:, 4])
    mh = R.metricas(sh, opens, opens, opens + H)
    assert np.isnan(mh["r4"][0]) and not np.isnan(mh["ret"][0])
    late = a[a[:, 0] >= t0 + 6 * MIN]
    sl = (late[:, 0].astype(np.int64), late[:, 1], late[:, 2], late[:, 3], late[:, 4])
    assert np.isnan(R.metricas(sl, opens, opens, opens + H)["ret"][0])


def test_encadenamiento_sin_mirar_el_futuro_y_el_solape():
    """São Paulo y Nueva York abren con 30 min de diferencia: mirar «la siguiente tal cual» mide casi lo mismo dos veces
    (el 60 % del 28-10). El encadenamiento del contrato empareja cada apertura con la primera que empieza cuando ya se
    sabe su resultado (≥ 4 h después)."""
    ops = [{"open_ms": 0, "id": "saopaulo"}, {"open_ms": 30 * MIN, "id": "nuevayork"},
           {"open_ms": 10 * H, "id": "tokio-seul"}, {"open_ms": 11 * H + 30 * MIN, "id": "china"}]
    r = {"BTC": np.array([0.01, 0.01, -0.01, 0.01])}
    p = {"BTC": np.array([0.005, 0.004, -0.003, np.nan])}
    ch = R.encadenamiento(ops, r, p)
    # operable: São Paulo → Tokio (+ → −), Nueva York → Tokio (+ → −); Tokio y China no tienen siguiente a ≥ 4 h
    assert (ch["after_up_rate"], ch["after_down_rate"], ch["base_rate"], ch["n"]) == (0.0, None, 0.0, 2)
    assert (ch["n_up"], ch["n_down"]) == (2, 0)
    # tal cual: (+ → +), (+ → −), (− → +): la mitad tras subida, pero es el solape
    assert ch["tal_cual"]["after_up_rate"] == 0.5 and ch["tal_cual"]["after_down_rate"] == 1.0
    assert ch["tal_cual"]["n"] == 3
    # parcial: lo que llevaba la anterior al abrir la siguiente (+0,5 → +, +0,4 → −, −0,3 → +)
    assert ch["parcial"]["n"] == 3 and ch["parcial"]["after_up_rate"] == 0.5
    assert "no predice" in ch["nota"] and "artefacto del solape" in ch["nota"]
    # São Paulo y Nueva York: 30 min en el verano de EE. UU., 90 desde el 1-11 (EE. UU. en invierno, Brasil sin cambio)
    assert "entre 30 y 90 min" in ch["nota"] and "30 min de diferencia" not in ch["nota"]
    assert R.open_ms_on("nuevayork", date(2026, 11, 2)) - R.open_ms_on("saopaulo", date(2026, 11, 2)) == 90 * MIN
    assert R.open_ms_on("nuevayork", date(2026, 10, 30)) - R.open_ms_on("saopaulo", date(2026, 10, 30)) == 30 * MIN
    assert R.encadenamiento([], {}, {}) is None


def test_calcular_hist_con_horarios_y_festivos():
    """Dos semanas con la Semana Dorada china (Shanghái cierra del 1 al 7-10-2026, Hong Kong el 1) y el Día de Hangul
    (Seúl, 9-10). Cada bolsa con su propia hora; Dubái, con su nota de festivos. La cripto sube un 0,6 % por hora."""
    start, end = date(2026, 9, 28), date(2026, 10, 9)
    t_a, t_b = ms(2026, 9, 27), ms(2026, 10, 11)
    rise = lambda t: 100.0 * math.exp(0.006 * (t - t_a) / H)
    data = bars(t_a, t_b, rise)
    loads = []

    def loader(c):
        loads.append(c)
        if c == "XRP":
            return None   # sin velas de XRP: su historia queda vacía y el resto sigue
        return data[:, 0].astype(np.int64), data[:, 1], data[:, 2], data[:, 3], data[:, 4]

    res = R.calcular_hist(loader, start, end, {}, clock=lambda: 123)
    assert loads == COINS and res["generated"] == 123 and res["version"] == R.VERSION
    n = {k: (h or {}).get("n") for k, h in res["markets"].items()}
    assert n == {"tokio": 10, "seul": 9, "shanghai": 5, "hongkong": 9, "dubai": 10, "frankfurt": 10, "londres": 10,
                 "madrid": 10, "nuevayork": 10, "saopaulo": 10}
    du, sh = res["markets"]["dubai"], res["markets"]["shanghai"]
    assert du["nota"] == R.NOTA_DUBAI and "nota" not in sh
    assert res["markets"]["tokio"]["desde"] == "2026-09-28" and set(sh["coins"]) == {"BTC", "ETH", "SOL"}
    b = sh["coins"]["BTC"]
    assert b["n"] == 5 and b["up_rate"] == 1.0 and b["range_ge2_rate"] == 1.0 and b["range_ge2_8h_rate"] == 1.0
    assert b["mean_ret_pct"] == pytest.approx((math.exp(0.024) - 1) * 100, abs=0.02)
    assert b["mean_range_pct"] == pytest.approx((math.exp(0.024) - 1) * 100, abs=0.02)
    assert sh["any_range_ge2_rate"] == 1.0
    # El total: las ventanas del laboratorio (backtest_aperturas.build_openings), 5 por día salvo China en su semana
    # El total: las ventanas del laboratorio (backtest_aperturas.build_openings), 5 por día; la de China solo falta el
    # 1-10, cuando cierran Shanghái y Hong Kong (con una de las dos abierta, la ventana cuenta).
    tot = res["total"]
    assert tot["n"] == 10 * 5 - 1 and tot["desde"] == "2026-09-28"
    ch = res["chain"]
    assert ch["after_up_rate"] == 1.0 and ch["base_rate"] == 1.0 and ch["after_down_rate"] is None


def test_sao_paulo_especial_fuera_de_la_historia():
    per, ops = R.lista_aperturas(date(2026, 2, 16), date(2026, 2, 20), {})
    # 16 y 17: Carnaval (festivos); 18: miércoles de ceniza (abre a las 13:00, no cuenta); 19 y 20 normales
    assert per["saopaulo"] == [ms(2026, 2, 19, 13, 0), ms(2026, 2, 20, 13, 0)]
    assert not any(o["id"] == "saopaulo" and o["day"] == "2026-02-18" for o in ops)
    assert ops == sorted(ops, key=lambda o: (o["open_ms"], o["id"]))


def test_periodo_de_3_anos_como_mucho():
    assert R.periodo(date(2026, 10, 28)) == (date(2023, 10, 28), date(2026, 10, 27))
    assert R.periodo(date(2028, 2, 29)) == (date(2025, 2, 28), date(2028, 2, 28))


# ---------------------------------------------------------------- módulo

def _series(c):
    t_a = ms(2026, 10, 19)
    a = bars(t_a, ms(2026, 10, 24), lambda t: 100.0 + (t - t_a) / H * 0.05)
    return a[:, 0].astype(np.int64), a[:, 1], a[:, 2], a[:, 3], a[:, 4]


async def test_modulo_publica_calcula_la_historia_en_un_hilo_y_la_reutiliza(tmp_path, monkeypatch):
    now = ms(2026, 10, 26, 9, 0)
    store = Store(tmp_path / "t.db")
    bus = Bus(store)
    loads = []
    path = tmp_path / "backtest" / R.HIST_FILE
    monkeypatch.setattr(R, "periodo", lambda today: (date(2026, 10, 19), date(2026, 10, 23)))
    live = {c: bars(ms(2026, 10, 25), now) for c in COINS}
    getter = lambda c, a, b: live[c][(live[c][:, 0] >= a) & (live[c][:, 0] < b)]
    r = R.Reaccion(bus, bars=getter, clock=lambda: now, loader=lambda c, d: loads.append(c) or _series(c),
                   index_loader=lambda: {}, path=path)
    assert r.hist is None and r.view()["markets"] == {}
    msg = await r.step()
    assert bus.state["cripto_aperturas"] is msg and msg["markets"]["londres"]["coins"]["BTC"]["ret_pct"] == 0.0
    assert r._task is not None
    await r._task                     # la historia, en un hilo
    await asyncio.sleep(0)
    assert loads == COINS and path.exists() and r.hist["total"]["n"] > 0 and r.err is None
    msg = await r.step()
    assert msg["chain"] == r.hist["chain"] and msg["markets"]["londres"]["hist"]["coins"]["BTC"]["up_rate"] == 1.0
    assert r.view() is msg
    # Otro arranque con el archivo de menos de 24 h: no se recalcula
    r2 = R.Reaccion(bus, bars=getter, clock=lambda: now + 3 * H, loader=lambda c, d: loads.append(c) or _series(c),
                    index_loader=lambda: {}, path=path)
    assert r2.hist["generated"] == now and not r2.hist_due(now + 3 * H)
    await r2.step()
    assert r2._task is None and len(loads) == 4
    # Pasadas 24 h toca otra vez
    assert r2.hist_due(now + DAY)
    store.close()


async def test_modulo_no_tumba_nada_si_fallan_la_historia_o_las_velas(tmp_path, caplog):
    now = ms(2026, 10, 26, 9, 0)
    store = Store(tmp_path / "t.db")
    bus = Bus(store)

    def broken_bars(c, a, b):
        raise OSError("caché ilegible")

    def broken_loader(c, d):
        raise ValueError("zip roto")

    (tmp_path / "bt").mkdir()
    (tmp_path / "bt" / R.HIST_FILE).write_text("{no es json", encoding="utf-8")   # archivo dañado: se ignora
    r = R.Reaccion(bus, bars=broken_bars, clock=lambda: now, loader=broken_loader, index_loader=lambda: 1 / 0,
                   path=tmp_path / "bt" / R.HIST_FILE)
    assert r.hist is None
    msg = await r.step()
    assert msg["markets"]["londres"]["state"] == "en_curso" and msg["markets"]["londres"]["coins"] is None
    await asyncio.gather(r._task, return_exceptions=True)
    await asyncio.sleep(0)
    assert r.hist is None and r.err and r._task is None
    await r.step()
    assert r._task is None   # no se reintenta enseguida (a la hora)
    assert "la historia no se pudo calcular" in caplog.text
    store.close()


async def test_api_cripto_aperturas_con_y_sin_el_modulo(tmp_path):
    store = Store(tmp_path / "t.db")
    bus = Bus(store)
    async with TestClient(TestServer(make_app(bus, lambda: {}))) as client:
        off = await (await client.get("/api/cripto_aperturas")).json()
        assert off["estado"] == "apagado" and off["markets"] == {} and off["coins"] == COINS
        bus.publish({"type": "cripto_aperturas", "ts": 1, "coins": COINS, "markets": {"tokio": {"state": "en_curso"}},
                     "chain": None})
        assert (await (await client.get("/api/cripto_aperturas")).json())["markets"]["tokio"]["state"] == "en_curso"
    mod = SimpleNamespace(view=lambda: {"type": "cripto_aperturas", "ts": 2, "markets": {"seul": {}}, "chain": None})
    async with TestClient(TestServer(make_app(bus, lambda: {}, Services(reaccion=mod)))) as client:
        assert (await (await client.get("/api/cripto_aperturas")).json())["markets"] == {"seul": {}}
    # El mensaje va también en el estado inicial del WebSocket (bus.snapshot)
    assert any(m.get("type") == "cripto_aperturas" for m in bus.snapshot())
    store.close()


def test_medida_completa_exige_las_4_h_sin_huecos():
    """Revisión del 28-10: un hueco de 2 h en medio de las 4 h no da un rango a medias; y la última vela tiene que abrir
    como mucho 5 min antes del final, como _ret en la historia."""
    t0 = ms(2026, 10, 26, 8, 0)
    full = bars(t0, t0 + 4 * H, price=lambda t: 100 + (t - t0) / H)   # sube 1 por hora: rango 4 %
    assert R.medida(full, t0, t0 + 4 * H, completa=True)["range_pct"] == pytest.approx(4.0, abs=0.01)
    hueco = full[(full[:, 0] < t0 + H) | (full[:, 0] >= t0 + 3 * H)]
    assert R.medida(hueco, t0, t0 + 4 * H, completa=True) is None
    assert R.medida(hueco, t0, t0 + 4 * H) is not None   # en curso se enseña lo que haya
    corta = full[full[:, 0] < t0 + 4 * H - 6 * MIN]        # la última abre 6 min antes del final: no vale
    assert R.medida(corta, t0, t0 + 4 * H, completa=True) is None
    casi = full[full[:, 0] < t0 + 4 * H - 4 * MIN]         # 5 min antes: vale
    assert R.medida(casi, t0, t0 + 4 * H, completa=True) is not None


def test_si_falla_el_guardado_la_historia_se_usa_igual(tmp_path, monkeypatch, caplog):
    now = ms(2026, 10, 26, 9, 0)
    monkeypatch.setattr(R, "periodo", lambda today: (date(2026, 10, 19), date(2026, 10, 23)))
    monkeypatch.setattr(R, "guardar_archivo", lambda path, res: 1 / 0)
    r = R.Reaccion(Bus(Store(tmp_path / "t.db")), bars=lambda c, a, b: None, clock=lambda: now,
                   loader=lambda c, d: _series(c), index_loader=lambda: {}, path=tmp_path / "h.json")
    res = r.compute_hist(now)
    assert res["total"]["n"] > 0 and "no pude guardar la historia" in caplog.text
