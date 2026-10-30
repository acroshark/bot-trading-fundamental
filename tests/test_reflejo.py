"""bot/reflejo.py: una noticia macro o geopolítica solo cuenta si la cripto la refleja (contrato 2.6, evento «reflejo»).
Reglas, los 4 estados, el módulo con el gancho del bus y la recuperación al arrancar, y su efecto en la señal del motor,
/api/noticias, el contexto del estratega y las predicciones. Sin red: velas sintéticas."""
import json
from types import SimpleNamespace

import numpy as np
import pytest

from bot import __main__ as main
from bot import apertura as ap
from bot import reflejo as RF
from bot.bus import Bus
from bot.server import recent_news
from bot.store import Store

MIN, H = RF.MIN, RF.H
COINS = RF.COINS
T0 = 1_793_000_000_000 // MIN * MIN + 20_000   # captura a los 20 s de un minuto
MACRO = {"tipo": "geopolítica", "temas": ["guerra"], "credibilidad": 80, "importancia": 7, "resumen": "Rusia lanza misiles",
         "repercusion": {"direccion": -60, "magnitud": 60, "activos": ["BTC"], "bolsas": []}}
CRIPTO = {**MACRO, "tipo": "exchange", "temas": ["cripto"], "resumen": "Hackeo en un exchange: roban ETH"}


def bars(moves: dict, t_from=T0 - 10 * MIN, t_to=T0 + 70 * MIN, base=100.0) -> dict:
    """Velas de 1 min por moneda; moves[c](minutos desde la captura) → % frente a base en el cierre de cada vela."""
    out = {}
    for c, f in moves.items():
        ts = np.arange(t_from // MIN * MIN, t_to, MIN, dtype=float)
        close = np.array([base * (1 + f((t + MIN - T0) / MIN) / 100) for t in ts])
        op = np.concatenate(([close[0]], close[:-1]))
        out[c] = np.column_stack([ts, op, np.maximum(op, close), np.minimum(op, close), close])
    return out


def step_at(pct: float, at_min: float):
    """La captura es a los 20 s de un minuto: las velas cierran a m = k − 1/3 min de ella. step_at(x, 15) llega en el
    cierre de las 16 − 1/3 (se informa redondeado: a los 16 min)."""
    return lambda m: 0.0 if m < at_min else pct


# ---------------------------------------------------------------- reglas

def test_que_noticias_necesitan_reflejo():
    assert RF.aplica("Rusia lanza más de 100 misiles balísticos contra Kiev", MACRO)
    assert RF.aplica("Nonfarm payrolls rise by 150,000", {**MACRO, "tipo": "macro", "temas": ["empleo"]})
    # Sin clase (historias de antes del 28-10-2026), de cripto solo por el titular: cuenta como siempre
    assert not RF.aplica("Bitcoin cae tras el ataque", MACRO)
    assert not RF.aplica("비트코인 급락", MACRO)
    assert not RF.aplica("Stablecoin issuer halts redemptions", MACRO)
    # El tipo, los temas y el resumen del triaje ya no hacen cripto a una noticia (hallazgo n.º 1 de la revisión)
    assert RF.aplica(None, CRIPTO) and RF.aplica("x", {**MACRO, "resumen": "La SEC aprueba el ETF de Solana"})
    # La clase del prefiltro manda sobre el titular
    assert not RF.aplica("Rusia lanza misiles", MACRO, "cripto") and RF.aplica("Bitcoin cae", MACRO, "macro")
    assert RF.aplica("x", MACRO, "contexto")
    # Sin dirección (neutral) no hace falta: no empuja
    assert not RF.aplica("Rusia y Ucrania reanudan conversaciones", {**MACRO, "repercusion": {"direccion": 10}})
    assert RF.direccion(MACRO) == -1 and RF.direccion({"repercusion": {"direccion": 40}}) == 1
    assert RF.direccion(None) == 0 and RF.direccion({"repercusion": {"direccion": "x"}}) == 0
    # Solo cuenta la macro confirmada; la de cripto siempre
    t = "Rusia lanza misiles"
    assert not RF.cuenta(t, MACRO, None) and not RF.cuenta(t, MACRO, {"estado": "pendiente"})
    assert not RF.cuenta(t, MACRO, {"estado": "contraria"}) and not RF.cuenta(t, MACRO, {"estado": "sin_reflejo"})
    assert RF.cuenta(t, MACRO, {"estado": "confirmada"}) and RF.cuenta("Bitcoin", MACRO, None)
    assert RF.cuenta(t, MACRO, None, "cripto") and not RF.cuenta("Bitcoin", MACRO, None, "macro")
    assert RF.macro_confirmada(t, MACRO, {"estado": "confirmada"}, "macro")
    assert not RF.macro_confirmada(t, MACRO, None, "macro") and not RF.macro_confirmada(t, MACRO, {"estado": "confirmada"})
    assert not RF.macro_confirmada("Bitcoin", MACRO, {"estado": "confirmada"}, "cripto")


# Titulares reales del 27-10-2026 que el triaje dejó con el tema «cripto» o el tipo «seguridad» o «institucional» sin
# que hablen de cripto: con la regla vieja se saltaban el reflejo y contaban sin confirmarse (el 12 % de las macro).
FUGAS = [
    ("Fed: keine Leitzinsänderung aber restriktiver Ton", {"tipo": "macro", "temas": ["banco_central", "cripto"]}),
    ("Morgan Stanley: Fed’in Bugünkü Kararı Faiz Artışlarını Bitirmiyor", {"tipo": "institucional", "temas": ["macro"]}),
    ("IDF kills three Hamas terrorists in Gaza", {"tipo": "seguridad", "temas": ["guerra"]}),
    ("IDCFクラウド攻撃、ランサムウェア被害の詳細を公表", {"tipo": "seguridad", "temas": ["tecnologia"]}),
]


@pytest.mark.parametrize("title,extra", FUGAS)
def test_macro_con_temas_o_tipo_cripto_del_triaje_necesita_reflejo(title, extra):
    tr = {**MACRO, **extra}
    for clase in ("macro", None):   # con la clase de la captada o, sin ella, por el titular
        assert not RF.es_cripto(title, clase) and RF.aplica(title, tr, clase)
        assert not RF.cuenta(title, tr, None, clase) and not RF.cuenta(title, tr, {"estado": "pendiente"}, clase)
        assert RF.cuenta(title, tr, {"estado": "confirmada"}, clase)


# ---------------------------------------------------------------- los 4 estados

def test_confirmada_contraria_sin_reflejo_y_pendiente():
    now = T0 + 70 * MIN
    # Bajista y la cripto cae un 0,6 % a los 15 min: confirmada (media de las cuatro)
    r = RF.medir(bars({c: step_at(-0.6, 15) for c in COINS}), T0, -1, now)
    assert r == {"estado": "confirmada", "mov_pct": pytest.approx(-0.6, abs=1e-6), "min": 16, "ref": "media",
                 "umbral_pct": 0.5}   # sin 24 h de velas previas no hay σ: el umbral es el mínimo de 0,5 %
    # Bajista y la cripto sube un 0,5 % (justo el umbral) a los 8 min: contraria
    r = RF.medir(bars({c: step_at(0.5, 8) for c in COINS}), T0, -1, now)
    assert (r["estado"], r["min"]) == ("contraria", 9) and r["mov_pct"] == pytest.approx(0.5)
    # Manda el primer toque: cae −0,6 a los 5 min y luego sube +1 %: para una alcista, contraria
    path = lambda m: -0.6 if 5 <= m < 20 else (1.0 if m >= 20 else 0.0)
    assert RF.medir(bars({c: path for c in COINS}), T0, 1, now)["estado"] == "contraria"
    # Movimientos pequeños durante los 60 min: sin reflejo, con lo que se movió a los 60 min
    r = RF.medir(bars({c: (lambda m: 0.3 * np.sin(m / 7)) for c in COINS}), T0, 1, now)
    assert r["estado"] == "sin_reflejo" and r["min"] == 60 and abs(r["mov_pct"]) < 0.5
    # Lo de después de los 60 min no cuenta
    assert RF.medir(bars({c: step_at(2.0, 61) for c in COINS}), T0, 1, now)["estado"] == "sin_reflejo"
    # Aún dentro de los 60 min: pendiente (con lo que lleva)
    b = bars({c: (lambda m: 0.2 if m > 0 else 0.0) for c in COINS}, t_to=T0 + 20 * MIN - 20_000)   # la última cierra a 19 min 40 s
    r = RF.medir(b, T0, 1, T0 + 20 * MIN)
    assert r["estado"] == "pendiente" and r["min"] == 20 and r["mov_pct"] == pytest.approx(0.2)
    # Si las velas no llegan, sin los 60 min completos no se cierra hasta 3 h después de la captura (pueden llegar
    # tarde: un hueco de velas o un reinicio); entonces, con lo que hay
    assert RF.medir(b, T0, 1, T0 + 69 * MIN)["estado"] == "pendiente"
    assert RF.medir(b, T0, 1, T0 + 71 * MIN)["estado"] == "pendiente"
    assert RF.medir(b, T0, 1, T0 + 3 * H + MIN)["estado"] == "sin_reflejo"


def test_media_de_las_cuatro_o_btc_si_faltan_datos():
    now = T0 + 70 * MIN
    moves = {"BTC": step_at(0.8, 10), "ETH": step_at(0.0, 0), "SOL": step_at(0.0, 0), "XRP": step_at(0.0, 0)}
    r = RF.medir(bars(moves), T0, 1, now)   # media: 0,2 %: no llega
    assert r["estado"] == "sin_reflejo" and r["ref"] == "media" and r["mov_pct"] == pytest.approx(0.2)
    b = bars(moves)
    del b["XRP"]
    r = RF.medir(b, T0, 1, now)              # falta XRP: solo BTC
    assert (r["estado"], r["ref"], r["min"]) == ("confirmada", "BTC", 11)
    b = bars(moves)
    b["SOL"] = b["SOL"][b["SOL"][:, 0] > T0 + 30 * MIN]   # SOL sin precio de referencia en la captura: BTC
    assert RF.medir(b, T0, 1, now)["ref"] == "BTC"
    assert RF.medir({}, T0, 1, now) is None and RF.medir({"ETH": bars({"ETH": step_at(1, 1)})["ETH"]}, T0, 1, now) is None
    # Una moneda que se queda sin velas nuevas (se paró su descarga) no corta la media: se mide con BTC sola
    caen = bars({c: step_at(-2.0, 5) for c in COINS})
    caen["XRP"] = caen["XRP"][caen["XRP"][:, 0] + MIN <= T0 + MIN]   # XRP se para en la captura
    r = RF.medir(caen, T0, -1, now)
    assert (r["estado"], r["ref"]) == ("confirmada", "BTC") and r["mov_pct"] == pytest.approx(-2.0)
    caen["XRP"] = bars({"XRP": step_at(-2.0, 5)})["XRP"]
    caen["XRP"] = caen["XRP"][caen["XRP"][:, 0] + MIN <= T0 + 4 * MIN]   # ... o a los 3 min
    assert RF.medir(caen, T0, -1, now)["ref"] == "BTC"


def _ruido(sd_pct_1m: float, n_min: int, seed: int = 1, t_end=T0 - MIN):
    """Velas de 1 min con rendimientos normales de sd_pct_1m % por minuto; la última abre en t_end (cierra antes de T0)."""
    rng = np.random.default_rng(seed)
    ts = np.arange(t_end // MIN * MIN - (n_min - 1) * MIN, t_end // MIN * MIN + MIN, MIN, dtype=float)
    close = 100 * np.exp(np.cumsum(rng.normal(0, sd_pct_1m / 100, len(ts))))
    op = np.concatenate(([close[0]], close[:-1]))
    return np.column_stack([ts, op, np.maximum(op, close), np.minimum(op, close), close])


def _pegar(antes: dict, despues: dict) -> dict:
    """Las velas de antes y, a continuación, las de después escaladas al último cierre (sin salto en la unión)."""
    out = {}
    for c in COINS:
        a = antes[c][antes[c][:, 0] < despues[c][0, 0]]
        d = despues[c].copy()
        d[:, 1:5] *= a[-1, 4] / d[0, 1]
        out[c] = np.vstack([a, d])
    return out


def test_umbral_el_mayor_de_0_5_y_2_sigma_de_60_min():
    """Hallazgo n.º 6: con el 0,5 % fijo, una de cada cinco noticias se «confirmaba» por azar. Ahora el umbral es el
    mayor de 0,5 % y 2σ del movimiento de 60 min de las 24 h previas a la captura (la misma referencia que se mide)."""
    calm = {c: _ruido(0.01, 1440, seed=k) for k, c in enumerate(COINS)}     # σ60 de la media < 0,1 %: manda el 0,5 %
    wild = {c: _ruido(0.15, 1440, seed=k) for k, c in enumerate(COINS)}     # σ60 de la media ≈ 0,6 %: umbral ≈ 1,2 %
    s_calm, s_wild = RF.sigma60(calm, T0, COINS), RF.sigma60(wild, T0, COINS)
    assert s_calm < 0.25 and 0.4 < s_wild < 1.5
    assert RF.umbral_de(calm, T0, COINS) == RF.UMBRAL_PCT
    assert RF.umbral_de(wild, T0, COINS) == pytest.approx(2 * s_wild)
    assert RF.sigma60({c: a[-300:] for c, a in wild.items()}, T0, COINS) is None   # pocas velas: sin σ, el mínimo
    # Con mucha volatilidad, un −0,6 % ya no confirma una noticia bajista; un movimiento mayor de 2σ, sí
    mov = bars({c: step_at(-0.6, 15) for c in COINS})
    b = _pegar(wild, mov)
    r = RF.medir(b, T0, -1, T0 + 70 * MIN)
    assert r["estado"] == "sin_reflejo" and r["umbral_pct"] == pytest.approx(2 * s_wild, abs=0.05) and r["umbral_pct"] > 0.6
    big = bars({c: step_at(-(2 * s_wild + 0.3), 15) for c in COINS})
    b = _pegar(wild, big)
    assert RF.medir(b, T0, -1, T0 + 70 * MIN)["estado"] == "confirmada"
    # Un umbral dado se respeta tal cual
    assert RF.medir(mov, T0, -1, T0 + 70 * MIN, umbral=1.0)["estado"] == "sin_reflejo"
    assert RF.AZAR["umbral_2sigma_pct"] < RF.AZAR["umbral_fijo_pct"] / 3


def test_textos_en_espanol():
    assert "confirma" in RF.texto({"estado": "confirmada", "mov_pct": -0.62, "min": 12, "ref": "media"}, -1)
    assert "−" not in RF.texto({"estado": "contraria", "mov_pct": 0.51, "min": 3, "ref": "BTC"}, -1)
    assert "0,5 %" in RF.texto({"estado": "sin_reflejo", "mov_pct": 0.1, "min": 60, "ref": "media"}, 1)
    assert "no cuenta" in RF.texto({"estado": "pendiente"}, 1).lower()
    assert "Sin velas" in RF.texto({"estado": "sin_reflejo", "mov_pct": None}, 1)
    t = RF.texto({"estado": "confirmada", "mov_pct": -1.2, "min": 9, "ref": "media", "umbral_pct": 0.93}, -1)
    assert "umbral 0,93 %" in t and "azar" in t and "no prueba" in t


# ---------------------------------------------------------------- módulo

def _world(tmp_path, data):
    store = Store(tmp_path / "t.db")
    bus = Bus(store)
    box = {"now": T0, "data": data}
    getter = lambda c, a, b: (box["data"].get(c) if box["data"].get(c) is not None else np.empty((0, 5)))
    rf = RF.Reflejo(bus, bars=getter, clock=lambda: box["now"])
    main.observe_bus(bus, rf.observe)
    return store, bus, rf, box


def _story(bus, sid, title, triage, t0, clase="macro"):
    bus.publish({"type": "event", "kind": "captured", "agent": "hunter", "story": sid, "ts": t0, "title": title,
                 "coins": [], "text": "", "patch": {"headline": title, "clase": clase, "tier": "T1"}})
    bus.publish({"type": "event", "kind": "triaged", "agent": "local", "story": sid, "ts": t0 + 5 * MIN, "title": title,
                 "coins": [], "text": "", "patch": {"triage": triage}})


def _reflejos(store, sid=None):
    evs = store.events_since(0, ("reflejo",))
    return [(e["story"], e["patch"]["reflejo"]["estado"]) for e in evs if sid is None or e["story"] == sid]


async def test_modulo_publica_pendiente_y_luego_el_estado_final(tmp_path):
    store, bus, rf, box = _world(tmp_path, bars({c: step_at(-0.7, 25) for c in COINS}, t_to=T0 + 6 * MIN))
    _story(bus, "m1", "Rusia lanza más de 100 misiles contra Kiev", MACRO, T0)
    _story(bus, "c1", "Hackeo en Upbit: roban 30 millones en ETH", CRIPTO, T0, clase="cripto")   # de cripto: no
    _story(bus, "n1", "Rusia y Ucrania hablan", {**MACRO, "repercusion": {"direccion": 0}}, T0)   # neutral: no
    assert set(rf.pending) == {"m1"} and rf.pending["m1"]["t0"] == T0 and rf.pending["m1"]["d"] == -1
    box["now"] = T0 + 6 * MIN
    assert await rf.step() == 0
    assert _reflejos(store) == [("m1", "pendiente")]
    ev = store.events_since(0, ("reflejo",))[0]
    assert ev["agent"] == "flow" and ev["title"].startswith("Rusia") and ev["coins"] == []
    assert set(ev["patch"]["reflejo"]) == {"estado", "mov_pct", "min", "ref", "umbral_pct"}
    await rf.step()
    assert _reflejos(store) == [("m1", "pendiente")]   # el pendiente sale una sola vez
    box["now"], box["data"] = T0 + 40 * MIN, bars({c: step_at(-0.7, 25) for c in COINS}, t_to=T0 + 40 * MIN)
    assert await rf.step() == 1
    assert _reflejos(store) == [("m1", "pendiente"), ("m1", "confirmada")] and not rf.pending
    final = store.events_since(0, ("reflejo",))[-1]["patch"]["reflejo"]
    assert final == {"estado": "confirmada", "mov_pct": pytest.approx(-0.7), "min": 26, "ref": "media", "umbral_pct": 0.5}
    v = rf.view()
    assert v["pendientes"] == 0 and v["finales"] == {"confirmada": 1} and v["tasa_confirmada"] == 1.0
    assert v["azar_confirmada_pct"] == RF.AZAR["umbral_2sigma_pct"]
    # Una triada repetida de la misma historia no vuelve a medirse
    bus.publish({"type": "event", "kind": "triaged", "agent": "local", "story": "m1", "ts": box["now"], "title": "x",
                 "patch": {"triage": MACRO}})
    assert not rf.pending
    store.close()


async def test_sin_velas_en_3_horas_queda_sin_reflejo(tmp_path):
    store, bus, rf, box = _world(tmp_path, {})
    _story(bus, "m1", "EE. UU. impone aranceles del 100 % a China", MACRO, T0)
    box["now"] = T0 + 10 * MIN
    await rf.step()
    assert _reflejos(store) == [("m1", "pendiente")]
    box["now"] = T0 + 3 * H + MIN
    await rf.step()
    assert _reflejos(store)[-1] == ("m1", "sin_reflejo")
    assert store.events_since(0, ("reflejo",))[-1]["patch"]["reflejo"]["mov_pct"] is None
    store.close()


async def test_al_arrancar_recupera_lo_pendiente(tmp_path):
    store, bus, rf, box = _world(tmp_path, {})
    _story(bus, "m1", "Irán cierra el estrecho de Ormuz", MACRO, T0)
    _story(bus, "m2", "Corea del Norte lanza un misil balístico", MACRO, T0)
    box["now"] = T0 + 10 * MIN
    await rf.step()
    bus.publish({"type": "event", "kind": "reflejo", "agent": "flow", "story": "m2", "ts": box["now"], "title": "x",
                 "patch": {"reflejo": {"estado": "contraria", "mov_pct": 0.6, "min": 4, "ref": "media"}}})
    # El bot se reinicia: otro Reflejo con la misma base y ya con velas
    data = bars({c: step_at(-0.9, 30) for c in COINS})
    rf2 = RF.Reflejo(bus, bars=lambda c, a, b: data[c], clock=lambda: T0 + 80 * MIN)
    await rf2.step()
    assert _reflejos(store, "m1") == [("m1", "pendiente"), ("m1", "confirmada")]   # sin repetir el pendiente
    assert _reflejos(store, "m2") == [("m2", "pendiente"), ("m2", "contraria")]    # la que ya tenía estado, igual
    # La hora de la captada sale de la base
    assert rf2.done["m1"] == "confirmada" and rf2.done["m2"] == "recuperada"
    store.close()


async def test_clase_de_la_captada_decide_aunque_no_este_en_memoria(tmp_path):
    """Una triada cuya captada no está en memoria (llegó antes de arrancar): la clase sale de la base antes de medir. Si
    es de cripto, se descarta sin publicar nada; si es macro, se mide."""
    store, bus, rf, box = _world(tmp_path, bars({c: step_at(-0.7, 25) for c in COINS}))
    for sid, clase in (("c1", "cripto"), ("m1", "macro")):
        store.add_event({"id": "cap-" + sid, "type": "event", "kind": "captured", "agent": "hunter", "story": sid, "ts": T0,
                         "title": "Rusia lanza misiles", "coins": [], "text": "", "patch": {"clase": clase}})
    for sid in ("c1", "m1"):
        bus.publish({"type": "event", "kind": "triaged", "agent": "local", "story": sid, "ts": T0 + 5 * MIN,
                     "title": "Rusia lanza misiles", "coins": [], "text": "", "patch": {"triage": MACRO}})
    assert set(rf.pending) == {"c1", "m1"} and rf.pending["c1"]["check"]
    box["now"] = T0 + 70 * MIN
    await rf.step()
    assert _reflejos(store) == [("m1", "confirmada")] and rf.done["c1"] == "cripto"
    assert RF.clases(store, ["c1", "m1", "nada"]) == {"c1": "cripto", "m1": "macro"}
    assert RF.clases(store.db, ["m1"]) == {"m1": "macro"} and RF.clases(SimpleNamespace(), ["m1"]) == {}
    store.close()


async def test_al_arrancar_recupera_7_horas_y_cierra_lo_que_no_se_puede_medir(tmp_path):
    """Al arrancar se recuperan las triadas de las últimas 7 h (la señal y la balanza de la web miran 6 h): las que ya
    no tienen velas quedan «sin_reflejo» y la web y el motor coinciden. Las de cripto (por su clase) no se tocan."""
    store = Store(tmp_path / "t.db")
    bus = Bus(store)
    now = T0 + 6 * H
    _story(bus, "m-vieja", "EE. UU. impone aranceles del 100 % a China", MACRO, T0 + 30 * MIN)   # hace 5,5 h
    _story(bus, "c-vieja", "Hackeo en Upbit", MACRO, T0 + 30 * MIN, clase="cripto")
    _story(bus, "m-antigua", "Irán cierra el estrecho de Ormuz", MACRO, T0 - 2 * H)              # hace 8 h: fuera
    rf = RF.Reflejo(bus, bars=lambda c, a, b: np.empty((0, 5)), clock=lambda: now)
    await rf.step()
    assert _reflejos(store) == [("m-vieja", "sin_reflejo")]
    assert store.events_since(0, ("reflejo",))[-1]["patch"]["reflejo"]["mov_pct"] is None
    store.close()


def test_el_gancho_nunca_lanza(tmp_path):
    store = Store(tmp_path / "t.db")
    rf = RF.Reflejo(Bus(store), bars=None)
    for m in (None, {}, {"type": "event"}, {"type": "event", "kind": "triaged", "story": "x", "patch": "roto"},
              {"type": "event", "kind": "captured", "story": "y", "ts": "no"}):
        rf.observe(m)
    store.close()


# ---------------------------------------------------------------- efecto en la señal, la API, el estratega

def _publish_macro(bus, sid, ts, d=-80, title="Rusia lanza más de 100 misiles contra Kiev", estado=None):
    bus.publish({"type": "event", "kind": "captured", "agent": "hunter", "story": sid, "ts": ts - 60_000, "title": title,
                 "coins": [], "text": "", "patch": {"headline": title, "src": "Reuters", "tier": "T1", "clase": "macro",
                                                    "url": "https://www.reuters.com/x"}})
    bus.publish({"type": "event", "kind": "triaged", "agent": "local", "story": sid, "ts": ts, "title": title, "coins": [],
                 "text": "", "patch": {"triage": {**MACRO, "resumen": title, "alcance": {"nivel": "global", "puntuacion": 90},
                                                  "repercusion": {"direccion": d, "magnitud": 80, "activos": [],
                                                                  "bolsas": []}}}})
    if estado:
        bus.publish({"type": "event", "kind": "reflejo", "agent": "flow", "story": sid, "ts": ts + 60_000, "title": title,
                     "coins": [], "text": "", "patch": {"reflejo": {"estado": estado, "mov_pct": -0.6, "min": 9,
                                                                     "ref": "media"}}})


def test_la_senal_del_motor_solo_cuenta_la_macro_confirmada(tmp_path):
    store = Store(tmp_path / "t.db")
    bus = Bus(store)
    now = 1_793_100_000_000
    get = ap.signal_getter(bus, clock=lambda: now, cache_s=0)
    win = {"markets": ["londres"], "open_ms": now + 10 * MIN}
    _publish_macro(bus, "m1", now - 30 * MIN)
    _publish_macro(bus, "m2", now - 30 * MIN, estado="sin_reflejo")
    _publish_macro(bus, "m3", now - 30 * MIN, estado="contraria")
    assert get("BTC", win)["drivers"] == []                # ni pendiente, ni sin reflejo, ni contraria
    _publish_macro(bus, "m4", now - 20 * MIN, estado="confirmada")
    s = get("BTC", win)
    assert "1 noticia" in s["drivers"][0]["factor"] and s["drivers"][0]["peso"] < 0
    # La de cripto cuenta sin reflejo
    bus.publish({"type": "event", "kind": "triaged", "agent": "local", "story": "c1", "ts": now - 10 * MIN,
                 "title": "Bitcoin ETF sees record inflows", "coins": ["BTC"], "text": "",
                 "patch": {"triage": {**CRIPTO, "resumen": "Entradas récord en el ETF de bitcoin",
                                      "alcance": {"nivel": "global", "puntuacion": 90},
                                      "repercusion": {"direccion": 70, "magnitud": 70, "activos": ["BTC"], "bolsas": []}}}})
    assert "2 noticias" in get("BTC", win)["drivers"][0]["factor"]
    store.close()


def test_api_noticias_lleva_el_reflejo_y_credibilidad_0_sin_confirmar(tmp_path):
    store = Store(tmp_path / "t.db")
    bus = Bus(store)
    from bot.models import now_ms
    now = now_ms()
    _publish_macro(bus, "m1", now - 30 * MIN)
    _publish_macro(bus, "m2", now - 30 * MIN, title="EE. UU. impone aranceles del 100 % a China", estado="confirmada")
    _publish_macro(bus, "m3", now - 30 * MIN, title="Irán cierra el estrecho de Ormuz", estado="contraria")
    by = {n["titular"]: n for n in recent_news(bus, 3, 0)}
    rusia, aranceles, iran = (by["Rusia lanza más de 100 misiles contra Kiev"],
                              by["EE. UU. impone aranceles del 100 % a China"], by["Irán cierra el estrecho de Ormuz"])
    assert rusia["reflejo"] is None and rusia["credibilidad_ajustada"] == 0 and rusia["clase"] == "macro"
    assert iran["reflejo"]["estado"] == "contraria" and iran["credibilidad_ajustada"] == 0
    assert aranceles["reflejo"]["estado"] == "confirmada" and aranceles["credibilidad_ajustada"] > 0
    json.dumps(by)
    store.close()


def test_contexto_del_estratega_sin_macro_no_confirmada(tmp_path):
    from bot import claude_agents as ca
    from bot.models import now_ms

    class Clock:
        def __call__(self):
            return now_ms()

    store = Store(tmp_path / "t.db")
    bus = Bus(store)
    p = ca.Phase2(bus, store, None, None, lambda: None, lambda: None, lambda: None, lambda: None, lambda: None,
                  lambda: None, lambda *a: True, client=SimpleNamespace(), clock=Clock())
    now = now_ms()
    _publish_macro(bus, "m1", now - 30 * MIN)
    _publish_macro(bus, "m2", now - 30 * MIN, title="EE. UU. impone aranceles del 100 % a China", estado="confirmada")
    news = p.recent_news()
    assert [n["resumen"] for n in news] == ["EE. UU. impone aranceles del 100 % a China"]
    assert news[0]["reflejo_cripto"] == "confirmada"
    store.close()


async def test_triada_mas_de_60_min_despues_de_captarla_queda_sin_reflejo_tarde(tmp_path):
    """Revisión del 28-10: lo que hizo la cripto en los 60 min tras la captura ya había pasado cuando el bot pudo usar la
    noticia (triada tarde): no se «confirma» ni pesa como fresca en la señal."""
    store, bus, rf, box = _world(tmp_path, bars({c: step_at(-0.9, 10) for c in COINS}, t_to=T0 + 5 * H))
    bus.publish({"type": "event", "kind": "captured", "agent": "hunter", "story": "m1", "ts": T0, "title": "Rusia",
                 "coins": [], "text": "", "patch": {"headline": "Rusia", "clase": "macro"}})
    bus.publish({"type": "event", "kind": "triaged", "agent": "local", "story": "m1", "ts": T0 + 5 * H, "title": "Rusia",
                 "coins": [], "text": "", "patch": {"triage": MACRO}})
    box["now"] = T0 + 5 * H + MIN
    assert await rf.step() == 1
    [ev] = store.events_since(0, ("reflejo",))
    assert ev["patch"]["reflejo"]["estado"] == "sin_reflejo" and ev["patch"]["reflejo"]["tarde"] is True
    assert "triada más de 60 min después" in ev["text"]
    store.close()


async def test_una_triada_que_llega_mientras_se_piden_las_velas_espera_a_la_ronda_siguiente(tmp_path):
    """Revisión del 28-10: la foto de las pendientes se hace antes de pedir las velas; la triada que llega durante la
    espera no se mide con velas que no cubren sus 24 h previas (su umbral se quedaría fijo en el 0,5 %)."""
    store, bus, rf, box = _world(tmp_path, {})
    _story(bus, "m1", "Rusia lanza misiles", MACRO, T0)
    box["now"] = T0 + 6 * MIN
    orig = rf._bars

    def lento(a, b):
        _story(bus, "m2", "Irán cierra Ormuz", MACRO, T0 + 3 * MIN)   # llega mientras se leen las velas
        rf._bars = orig
        return orig(a, b)

    rf._bars = lento
    await rf.step()
    assert "m2" in rf.pending and rf.pending["m2"]["umbral"] is None and not rf.pending["m2"]["sent"]
    store.close()


async def test_api_reflejo_compara_con_el_azar(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from bot.server import Services, make_app
    store, bus, rf, box = _world(tmp_path, {})
    rf.counts.update({"confirmada": 1, "sin_reflejo": 3})
    for sv, estado in ((Services(reflejo=rf), "encendido"), (Services(), "apagado")):
        client = TestClient(TestServer(make_app(bus, lambda: {}, sv)))
        await client.start_server()
        try:
            res = await (await client.get("/api/reflejo")).json()
        finally:
            await client.close()
        assert res["estado"] == estado and res["azar_confirmada_pct"] == RF.AZAR["umbral_2sigma_pct"]
        if estado == "encendido":
            assert res["tasa_confirmada"] == 0.25 and res["finales"] == {"confirmada": 1, "sin_reflejo": 3}
    store.close()
