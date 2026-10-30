"""Entrada por noticia fiable confirmada por el precio (bot/entrada_noticias.py): espera, confirmación, entrada por el
camino del motor, vetos (una posición por moneda, topes del día compartidos, reglas del usuario), caducidad, macro con
reflejo, arranque y sin grupos fiables. Sin red."""
from datetime import datetime, timezone

import numpy as np
import pytest

from bot import apertura as ap, config, entrada_noticias as en, fiables
from bot.bus import Bus
from bot.store import Store

MIN, H = 60_000, 3_600_000
TS = int(datetime(2026, 10, 28, 10, 0, 20, tzinfo=timezone.utc).timestamp() * 1000)   # triaje
TA = en.ta_de(TS)                                                                      # 10:01 UTC
GRUPO = {"clave": "medio:bueno.com", "tipo": "medio", "nombre": "bueno.com", "sentido": None, "n": 40, "dias": 4,
         "acierto": 0.7, "ic90_bajo": 0.58, "estado": "fiable", "outlet": "bueno.com", "n_historias": 40,
         "opera": True}


@pytest.fixture(autouse=True)
def con_fiables():
    fiables.usar({"grupos": [GRUPO]})
    yield
    fiables.usar(None)


def velas(d=1, paso=0.06, desde=TA - 25 * H, hasta=TA + 3 * H, coin_px=100.0):
    """Velas de 1 min planas hasta ta y, después, a favor de d paso % por minuto (cierre de la vela k tras ta)."""
    rows = []
    for t in range(desde, hasta, MIN):
        k = (t + MIN - TA) // MIN
        c = coin_px * (1 + d * paso / 100 * k) if k > 0 else coin_px
        rows.append([t, c, c, c, c, 1.0])
    return np.array(rows, float)


def bars_fn(now, series):
    def get(coin, a, b):
        x = series.get(coin)
        if x is None:
            return None
        return x[(x[:, 0] >= a) & (x[:, 0] < b) & (x[:, 0] + MIN <= now[0])]
    return get


def mod(tmp_path, now, series=None, pf=None, sent=None, prices=None, enabled=True, motor=None):
    store = Store(tmp_path / "bot.db")
    bus = Bus(store)
    pf = {"positions": [], "pending": [], "scheduled": [], "closed": [], "equity": 111.37} if pf is None else pf
    sent = [] if sent is None else sent
    bus.state["market"] = {"prices": prices or {"BTC": 100.6, "ETH": 99.4}, "updated": {}, "flow": {}}
    m = en.EntradaNoticias(bus, store, lambda *a: sent.append(a) or True, lambda: pf,
                           bars=bars_fn(now, series or {"BTC": velas()}), clock=lambda: now[0], enabled=enabled,
                           get_motor=(lambda: motor) if motor is not None else None)
    publish = bus.publish

    def publish_and_observe(msg):   # como __main__.observe_bus
        out = publish(msg)
        m.observe(msg)
        return out

    bus.publish = publish_and_observe
    return m, bus, store, sent


def noticia(bus, sid="n1", d=60, coins=("BTC",), url="https://bueno.com/a", clase="cripto", ts=TS, tipo="exchange",
            title="Bitcoin: noticia"):
    bus.publish({"type": "event", "kind": "captured", "agent": "hunter", "story": sid, "ts": ts - 5000, "title": title,
                 "patch": {"headline": title, "src": "Bueno", "srcId": "feed-bueno", "url": url, "clase": clase}})
    bus.publish({"type": "event", "kind": "triaged", "agent": "local", "story": sid, "ts": ts, "title": title,
                 "patch": {"triage": {"tipo": tipo, "importancia": 7, "credibilidad": 80, "resumen": title,
                                      "repercusion": {"direccion": d, "magnitud": 70, "activos": list(coins)},
                                      "monedas": list(coins)}}})


def eventos(bus, store, kind="entrada_noticia"):
    return [e for e in store.events_since(0, (kind,))]


async def test_espera_confirma_y_entra_como_el_motor(tmp_path):
    now = [TA + MIN]
    m, bus, store, sent = mod(tmp_path, now)
    noticia(bus)
    await m.step()
    [esp] = eventos(bus, store)
    assert esp["story"] == "n1" and esp["patch"]["entrada_noticia"]["estado"] == "esperando" and not sent
    assert esp["coins"] == ["BTC"] and esp["patch"]["entrada_noticia"]["grupo"]["clave"] == "medio:bueno.com"
    now[0] = TA + 9 * MIN + 40_000   # +0,54 % al cierre de las 10:10 (umbral 0,5 %: sin σ, el mínimo)
    assert await m.step() == 1
    [(t, story, hl, strat, win)] = sent
    assert story == "nf-n1-btc" and strat == "noticia-fiable-largo" and win == "Noticia fiable" and "bueno.com" in hl
    assert t["side"] == "long" and t["instrument"] == "spot" and t["timing"] == "now" and t["risk"] == 0.5
    assert t["stopPct"] == 1.5 and t["tp1R"] == t["tp2R"] == 3.0 and t["tp1Frac"] == 0 and t["beR"] is None
    assert t["trailPct"] is None and t["trailStartR"] is None and t["winEnd"] == now[0] + 24 * H
    assert t["origen"] == "noticia_fiable" and t["noticia"] == "n1" and t["maxAgeMs"] == ap.MAX_FILL_DELAY_MS
    assert t["tags"]["origen"] == "noticia_fiable" and t["tags"]["story"] == "n1" and t["mode"] == "A favor de la noticia"
    assert t["tags"]["umbral_pct"] == 0.5 and t["tags"]["mov_pct"] == pytest.approx(0.54) and t["holdLabel"] == "tope de 24 h"
    [st] = eventos(bus, store, "strategy")   # el mismo camino que el motor: evento «strategy» y luego el paper
    assert st["story"] == "nf-n1-btc" and st["patch"]["strat"]["stopPct"] == 1.5 and st["patch"]["noticia"] == "n1"
    fin = eventos(bus, store)[-1]["patch"]["entrada_noticia"]
    assert fin["estado"] == "confirmada" and fin["paper_story"] == "nf-n1-btc" and fin["entrada"] == 100.6
    assert fin["stop"] == pytest.approx(100.6 * 0.985) and fin["objetivo"] == pytest.approx(100.6 * 1.045)
    assert await m.step() == 0 and len(sent) == 1   # no se repite
    store.close()


async def test_corto_en_perpetuo(tmp_path):
    now = [TA + 9 * MIN + 30_000]
    m, bus, store, sent = mod(tmp_path, now, {"ETH": velas(-1)})
    noticia(bus, d=-70, coins=("ETH",), title="Ethereum: hackeo")
    assert await m.step() == 1
    [(t, story, _hl, strat, _w)] = sent
    assert t["side"] == "short" and t["instrument"] == "perp" and strat == "noticia-fiable-corto" and story == "nf-n1-eth"
    store.close()


async def test_vetos_una_por_moneda_y_topes_compartidos(tmp_path):
    now = [TA + 9 * MIN + 30_000]
    pf = {"positions": [{"story": "a-europa-2026-10-28-btc-1", "coin": "BTC", "side": "short", "opened": TA}],
          "pending": [], "scheduled": [], "closed": []}
    m, bus, store, sent = mod(tmp_path, now, pf=pf)
    noticia(bus)
    await m.step()
    assert not sent
    fin = eventos(bus, store)[-1]["patch"]["entrada_noticia"]
    assert fin["estado"] == "vetada" and "una sola posición por moneda" in fin["motivo"]
    # Seis entradas del paper hoy (del motor o de otras): el tope del día es compartido
    hoy = ap.madrid_day_start(now[0])
    closed = [{"story": f"a-china-2026-10-28-{c}-{k}", "coin": "ETH", "opened": hoy + k * MIN, "riskUsd": 0.1}
              for k, c in enumerate("abcdef")]
    pf2 = {"positions": [], "pending": [], "scheduled": [], "closed": closed, "equity": 111.37}
    m2, bus2, store2, sent2 = mod(tmp_path / "b", now, pf=pf2)
    noticia(bus2)
    await m2.step()
    assert not sent2 and "tope de 6 entradas" in eventos(bus2, store2)[-1]["patch"]["entrada_noticia"]["motivo"]
    # Riesgo del día: 5 entradas con 0,6 % cada una + 0,5 % > 3 %
    pf3 = {**pf2, "closed": [{**x, "riskUsd": 0.6 * 111.37 / 100} for x in closed[:5]]}
    m3, bus3, store3, sent3 = mod(tmp_path / "c", now, pf=pf3)
    noticia(bus3)
    await m3.step()
    assert not sent3 and "riesgo del día agotado" in eventos(bus3, store3)[-1]["patch"]["entrada_noticia"]["motivo"]
    # El motor cuenta lo mismo: las entradas de la noticia también le cuentan (apertura.paper_day_entries)
    pf4 = {"positions": [{"story": "nf-n9-sol", "coin": "SOL", "opened": hoy + MIN, "riskUsd": 0.55}],
           "pending": [{"story": "nf-n8-xrp", "coin": "XRP"}], "closed": [], "equity": 111.37}
    assert ap.paper_day_entries(pf4, now[0]) == {"nf-n9-sol": round(0.55 / 111.37 * 100, 4),
                                                 "nf-n8-xrp": config.RISK_PER_TRADE_PCT}
    # Con el motor en E2 (4 entradas y 2 % al día) los topes compartidos son los suyos, no los de «6 diarias»
    pf5 = {**pf2, "closed": closed[:4]}
    m5, bus5, store5, sent5 = mod(tmp_path / "e", now, pf=pf5, motor=ap.IMPULSO)
    noticia(bus5)
    await m5.step()
    assert not sent5 and "tope de 4 entradas" in eventos(bus5, store5)[-1]["patch"]["entrada_noticia"]["motivo"]
    # ... y con «6 diarias», esas 4 dejan sitio
    m6, bus6, store6, sent6 = mod(tmp_path / "f", now, pf=pf5, motor=ap.SEIS)
    noticia(bus6)
    assert await m6.step() == 1 and len(sent6) == 1
    for s in (store, store2, store3, store5, store6):
        s.close()


def test_topes_del_dia_son_los_del_motor_y_nunca_mas_de_6_y_3():
    assert en.topes_dia(None) == (6, 3.0) and en.topes_dia({}) == (6, 3.0)
    assert en.topes_dia(ap.IMPULSO) == (4, 2.0) and en.topes_dia(ap.SEIS) == (6, 3.0)
    assert en.topes_dia({"max_day_entries": 10, "max_day_risk_pct": 5.0}) == (6, 3.0)
    assert "tope de 4" in en.limites_dia({str(k): 0.1 for k in range(4)}, 0.5, 4, 2.0)
    assert "riesgo del día" in en.limites_dia({"a": 1.0, "b": 0.6}, 0.5, 4, 2.0)


async def test_caduca_sin_movimiento_y_no_persigue_tarde(tmp_path):
    now = [TA + MIN]
    m, bus, store, sent = mod(tmp_path, now, {"BTC": velas(paso=0.0)})
    noticia(bus)
    await m.step()
    now[0] = TA + 30 * MIN
    await m.step()
    assert eventos(bus, store)[-1]["patch"]["entrada_noticia"]["estado"] == "esperando"
    now[0] = TA + 61 * MIN
    await m.step()
    fin = eventos(bus, store)[-1]["patch"]["entrada_noticia"]
    assert fin["estado"] == "caducada" and "no se movió a favor" in fin["motivo"] and not sent
    # Confirmó a las 10:10, pero el bot no lo vio hasta las 10:31: no se persigue el precio
    late = [TA + 30 * MIN]
    m2, bus2, store2, sent2 = mod(tmp_path / "b", late)
    noticia(bus2)
    await m2.step()
    fin = eventos(bus2, store2)[-1]["patch"]["entrada_noticia"]
    assert fin["estado"] == "caducada" and "llegaron tarde" in fin["motivo"] and not sent2
    store.close()
    store2.close()


async def test_sin_grupos_fiables_o_apagada_no_hace_nada(tmp_path):
    fiables.usar(None)
    now = [TA + 9 * MIN + 30_000]
    m, bus, store, sent = mod(tmp_path, now)
    noticia(bus)
    assert await m.step() == 0 and not sent and not eventos(bus, store)
    fiables.usar({"grupos": [GRUPO]})
    m2, bus2, store2, sent2 = mod(tmp_path / "b", now, enabled=False)
    noticia(bus2)
    assert await m2.step() == 0 and not sent2 and not eventos(bus2, store2)
    # De un medio que no es fiable: nada
    m3, bus3, store3, sent3 = mod(tmp_path / "c", now)
    noticia(bus3, url="https://otro.com/a")
    assert await m3.step() == 0 and not sent3 and not eventos(bus3, store3)
    for s in (store, store2, store3):
        s.close()



async def test_macro_espera_al_reflejo_y_vale_para_las_cuatro(tmp_path):
    now = [TA + MIN]
    series = {c: velas(-1) for c in config.COINS}
    m, bus, store, sent = mod(tmp_path, now, series)
    noticia(bus, d=-60, coins=(), clase="macro", tipo="macro", title="La Fed sube los tipos")
    await m.step()
    assert not eventos(bus, store) and "n1" in m.waiting   # sin reflejo todavía: espera
    bus.publish({"type": "event", "kind": "reflejo", "agent": "flow", "story": "n1", "ts": TA + 2 * MIN,
                 "title": "x", "patch": {"reflejo": {"estado": "confirmada", "mov_pct": -0.6, "min": 2}}})
    now[0] = TA + 3 * MIN
    await m.step()
    esp = [e for e in eventos(bus, store) if e["patch"]["entrada_noticia"]["estado"] == "esperando"]
    assert sorted(e["coins"][0] for e in esp) == sorted(config.COINS) and "n1" not in m.waiting
    # Una macro que la cripto no confirma: vetada (una vez, sin moneda)
    m2, bus2, store2, _ = mod(tmp_path / "b", now, series)
    noticia(bus2, sid="n2", d=-60, coins=(), clase="macro", tipo="macro", title="La Fed sube los tipos")
    bus2.publish({"type": "event", "kind": "reflejo", "agent": "flow", "story": "n2", "ts": TA, "title": "x",
                  "patch": {"reflejo": {"estado": "sin_reflejo", "mov_pct": 0.1, "min": 60}}})
    await m2.step()
    [v] = eventos(bus2, store2)
    assert v["patch"]["entrada_noticia"]["estado"] == "vetada" and v["coins"] == [] and "no confirmó" in v["text"]
    store.close()
    store2.close()


async def test_arranque_no_repite_lo_ya_resuelto(tmp_path):
    now = [TA + 9 * MIN + 30_000]
    m, bus, store, sent = mod(tmp_path, now)
    noticia(bus)
    assert await m.step() == 1
    store.close()
    # Reinicio: las mismas triadas y eventos están en la base; nada se vuelve a enviar ni a publicar
    now[0] += 20_000
    store2 = Store(tmp_path / "bot.db")
    bus2 = Bus(store2)
    bus2.state["market"] = {"prices": {"BTC": 100.6}, "updated": {}, "flow": {}}
    sent2 = []
    pf = {"positions": [], "pending": [], "scheduled": [], "closed": []}
    m2 = en.EntradaNoticias(bus2, store2, lambda *a: sent2.append(a) or True, lambda: pf,
                            bars=bars_fn(now, {"BTC": velas()}), clock=lambda: now[0])
    n_ev = len(eventos(bus2, store2))
    assert await m2.step() == 0 and not sent2 and len(eventos(bus2, store2)) == n_ev
    assert m2.done[("n1", "BTC")] == "confirmada"
    store2.close()


async def test_sin_grupos_que_operan_la_cola_espera_y_no_se_pierde(tmp_path):
    fiables.usar({"grupos": [{**GRUPO, "opera": False}]})   # fiable, pero sin pasar la puerta para operar
    now = [TA + MIN]
    m, bus, store, sent = mod(tmp_path, now)
    noticia(bus)
    assert await m.step() == 0 and not eventos(bus, store) and len(m.queue) == 1   # espera en la cola
    fiables.usar({"grupos": [GRUPO]})   # llega el cálculo (p. ej., el primero tras arrancar)
    await m.step()
    [esp] = eventos(bus, store)
    assert esp["patch"]["entrada_noticia"]["estado"] == "esperando" and not m.queue
    assert "40 noticias distintas" in esp["text"]
    # sin grupos que operan y ya fuera de tiempo: se descarta de la cola
    fiables.usar(None)
    m2, bus2, store2, _ = mod(tmp_path / "b", now)
    noticia(bus2)
    await m2.step()
    now[0] = TA + 70 * MIN
    await m2.step()
    assert not m2.queue and not eventos(bus2, store2)
    store.close()
    store2.close()


def velas_vol(desde, hasta=TA + 3 * H):
    """Velas que suben y bajan 0,05 % cada minuto hasta ta (saltos de 0,1 %: 2σ de 60 min ≈ 1,55 %) y planas después."""
    rows = []
    for t in range(desde, hasta, MIN):
        c = 100.0 * (1 + (0.0005 if (t // MIN) % 2 else -0.0005)) if t + MIN <= TA else 100.0
        rows.append([t, c, c, c, c, 1.0])
    return np.array(rows, float)


async def test_umbral_se_recalcula_hasta_tener_sigma(tmp_path):
    now = [TA + MIN]
    series = {"BTC": velas_vol(TA - 2 * H)}   # aún no hay 24 h de velas: sin σ, 0,5 % provisional
    m, bus, store, sent = mod(tmp_path, now, series)
    noticia(bus)
    await m.step()
    c = m.cands[("n1", "BTC")]
    assert c["umbral"] == 0.5 and c["umbral_fijo"] is False
    series["BTC"] = velas_vol(TA - 25 * H)   # llegan las 24 h anteriores: 2σ manda
    now[0] = TA + 2 * MIN
    await m.step()
    assert c["umbral"] == pytest.approx(1.549, abs=0.01) and c["umbral_fijo"] is True
    assert en.umbral_de(velas(), "BTC", TA) == (0.5, True)   # precio plano: σ = 0, el mínimo, fijo
    assert en.umbral_de(velas(desde=TA - H), "BTC", TA) == (0.5, False)
    store.close()


def test_reglas_del_usuario_siempre_y_limites():
    ok = en.plan_de(1, 100.0)
    assert not config.USER_RULES and en.reglas_ko(ok) is None   # se comprueban aunque BOT_USER_RULES=0
    assert ok["stopPct"] == 1.5 and ok["rr"] == 3.0 and ok["net_pct"] >= 3.0 and ok["instrument"] == "spot"
    assert "stop" in en.reglas_ko({**ok, "stopPct": 1.0})
    assert "R:R" in en.reglas_ko({**ok, "rr": 1.5})
    assert "neto" in en.reglas_ko({**ok, "net_pct": 2.5})
    assert "break-even" in en.reglas_ko({**ok, "beR": 1.0})
    assert en.plan_de(-1, 100.0)["instrument"] == "perp"
    assert "tope de 6" in en.limites_dia({str(k): 0.1 for k in range(6)}, 0.5)
    assert en.limites_dia({str(k): 0.5 for k in range(5)}, 0.5) is None
    assert "riesgo del día" in en.limites_dia({str(k): 0.6 for k in range(5)}, 0.5)


def test_confirmar_puro():
    b = velas()
    r = en.confirmar(b, TA, 1, 0.5, TA + 20 * MIN)
    assert r["estado"] == "confirmada" and r["min"] == 9 and r["t"] == TA + 9 * MIN and r["ref"] == 100.0
    assert en.confirmar(b, TA, -1, 0.5, TA + 20 * MIN)["estado"] == "esperando"   # va en contra: sigue esperando
    assert en.confirmar(b, TA, -1, 0.5, TA + 70 * MIN)["estado"] == "caducada"
    assert en.confirmar(None, TA, 1, 0.5, TA + 5 * MIN)["estado"] == "esperando"
    assert en.confirmar(None, TA, 1, 0.5, TA + 64 * MIN)["estado"] == "caducada"


async def test_envio_que_el_paper_no_abrio_deja_de_contar_en_el_tope(tmp_path):
    """Revisión del 28-10: una entrada enviada que el paper no llega a abrir (rechazada o bloqueada al ejecutarse) deja de
    contar para los topes del día pasado el plazo del motor (SUBMIT_GRACE_MS), como en el motor."""
    now = [TA + 9 * MIN + 30_000]
    m, bus, store, sent = mod(tmp_path, now)
    noticia(bus)
    assert await m.step() == 1   # enviada, pero la cartera de prueba (pf fijo) nunca la enseña
    own = m._own_today(now[0])
    assert list(own) == ["nf-n1-btc"] and own["nf-n1-btc"][0] == 0.5
    pf = {"positions": [], "pending": [], "scheduled": [], "closed": [], "equity": 111.37}
    now[0] += ap.SUBMIT_GRACE_MS + 1000
    m.get_portfolio = lambda: pf
    noticia(bus, sid="n2", coins=("ETH",), title="Ethereum: noticia", ts=now[0] - 9 * MIN - 30_000)
    m.bars = bars_fn(now, {"ETH": velas(desde=now[0] - 9 * MIN - 25 * H, hasta=now[0] + H)})
    await m.step()
    assert "nf-n1-btc" not in m._own_today(now[0])
    store.close()
