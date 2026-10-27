import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from bot import claude_agents as ca
from bot import config
from bot.bus import Bus
from bot.store import Store

UTC = timezone.utc
HAIKU, SONNET, OPUS = config.CLAUDE_VERIFIER_MODEL, config.CLAUDE_IMPACT_MODEL, config.CLAUDE_STRATEGIST_MODEL
# Lunes 26-10-2026: Londres ya está en horario de invierno (apertura 08:00 UTC = 09:00 Madrid).
LONDON = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)
PRE_LONDON = datetime(2026, 10, 26, 7, 46, tzinfo=UTC)      # 14 min antes: toca preparar la ventana
IN_LONDON = datetime(2026, 10, 26, 8, 10, tzinfo=UTC)       # ventana abierta
VERIFY_KEYS = {"score", "checks", "why", "dt", "llm"}
IMPACT_KEYS = {"dir", "mag", "conf", "hz", "why", "main", "per", "markets", "llm"}
LLM_KEYS = ["model", "key", "tin", "cached", "tout", "cost"]


def ms(dt):
    return int(dt.timestamp() * 1000)


# ---------------------------------------------------------------- dobles sin red

def usage(tin=1000, read=2000, write=500, tout=300, iterations=None):
    return SimpleNamespace(input_tokens=tin, cache_read_input_tokens=read, cache_creation_input_tokens=write,
                           output_tokens=tout, iterations=iterations)


def reply(data, model, stop="end_turn", u=None, category=None):
    content = [] if data is None else [SimpleNamespace(type="thinking", thinking="", signature="s"),
                                       SimpleNamespace(type="text", text=json.dumps(data))]
    return SimpleNamespace(model=model, stop_reason=stop, usage=u or usage(), content=content,
                           stop_details=SimpleNamespace(category=category) if category else None)


class APIErr(Exception):
    """Error de la API con su código, como los APIStatusError del SDK."""

    def __init__(self, status, msg="error"):
        super().__init__(msg)
        self.status_code = status


class FakeAPI:
    def __init__(self, fn, calls, beta, delay=0.0):
        self.fn, self.calls, self.beta, self.delay = fn, calls, beta, delay

    async def create(self, **kw):
        self.calls.append({"beta": self.beta, **kw})
        if self.delay:
            await asyncio.sleep(self.delay)
        out = self.fn(kw)
        if isinstance(out, Exception):
            raise out
        return out


class FakeClient:
    def __init__(self, fn, delay=0.0):
        self.calls, self.options, self.closed = [], [], False
        self.messages = FakeAPI(fn, self.calls, False, delay)
        self.beta = SimpleNamespace(messages=FakeAPI(fn, self.calls, True, delay))

    def with_options(self, **kw):
        self.options.append(kw)
        return self

    async def close(self):
        self.closed = True


VERIFY_OK = {"checks": [{"ok": True, "t": "Comunicado oficial de la SEC (T0)"}, {"ok": True, "t": "Confirmada por Reuters"}],
             "why": "Comunicado oficial confirmado", "score": 0.92}
IMPACT_BIG = {"why": "La aprobación abre la puerta a flujos institucionales.", "main": "SOL", "dir": 1, "mag": 2.1, "conf": 0.7,
              "hz": "1-4 h", "per": [{"coin": "SOL", "pct": 2.1}, {"coin": "ETH", "pct": -0.6}],
              "markets": [{"market": "nuevayork", "pct": 0.2}, {"market": "atlantis", "pct": 9}]}
STRAT_4 = {"why": "Asia cerró al alza y el flujo acompaña.", "tickets": [
    {"coin": "BTC", "side": "long", "mode": "Seguir bolsa anterior", "risk": 2.0, "stopPct": 0.9, "trailPct": 0.8,
     "tp1R": 1.5, "tp2R": 3, "inval": "BTC pierde 95.000", "why": "Tokio y Seúl cerraron en positivo."},
    {"coin": "ETH", "side": "short", "mode": "Contra el minorista extremo", "risk": 0.3, "stopPct": 1.2, "trailPct": 0,
     "tp1R": 1.5, "tp2R": 3, "inval": "ETH recupera el VWAP", "why": "Percentil 95 de largos."},
    {"coin": "BTC", "side": "short", "mode": "Seguir la tendencia", "risk": 0.2, "stopPct": 1, "trailPct": 1,
     "tp1R": 1.5, "tp2R": 3, "inval": "x", "why": "duplicada"},
    {"coin": "SOL", "side": "long", "mode": "Invento", "risk": 0.4, "stopPct": 1.6, "trailPct": 1.4,
     "tp1R": 2, "tp2R": 1, "inval": "SOL pierde el rango", "why": "Predicción del 66 %."},
    {"coin": "XRP", "side": "long", "mode": "Predicción de la ventana", "risk": 0.4, "stopPct": 1.5, "trailPct": 1.5,
     "tp1R": 1.5, "tp2R": 3, "inval": "x", "why": "sobra"}]}


def by_model(verify=VERIFY_OK, impact=IMPACT_BIG, strat=STRAT_4):
    def fn(kw):
        data = {HAIKU: verify, SONNET: impact, OPUS: strat}[kw["model"]]
        return data if isinstance(data, SimpleNamespace) else reply(data, kw["model"])
    return fn


class FakeHTTP:
    def __init__(self, data):
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    async def json(self):
        return {"choices": [{"message": {"content": json.dumps(self.data)}}],
                "usage": {"prompt_tokens": 900, "completion_tokens": 120}}


class FakeSession:
    def __init__(self, fn):
        self.fn, self.calls = fn, []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        return FakeHTTP(self.fn(json))


LOCAL_VERIFY = {"checks": [{"ok": True, "t": "Fuente T0"}], "why": "Oficial", "score": 0.8}
LOCAL_IMPACT = {"why": "Local", "main": "SOL", "dir": 1, "mag": 1.0, "conf": 0.6, "hz": "1-4 h", "per": [], "markets": []}


class FakeLocalAI:
    def __init__(self, ok=True):
        self.base = "http://127.0.0.1:8094"
        self.s = FakeSession(lambda body: {"verificacion": LOCAL_VERIFY, "impacto": LOCAL_IMPACT}[
            body["response_format"]["json_schema"]["name"]])
        self.last_use = 0
        self.ok = ok
        self.acquired = 0

    async def acquire(self):   # LocalAI.acquire: _ready() y, con el servidor de la fábrica, esperar a que esté libre
        self.acquired += 1
        return self.ok


PREDICTIONS = {"type": "predictions", "items": [
    {"id": "BTC-lon", "kind": "cripto", "target": "BTC", "label": "BTC · Apertura Londres", "event": "ventana",
     "when_utc": ms(LONDON), "horizon": "1h", "prob_up": 0.65, "confidence": "media",
     "drivers": [{"factor": "Flujo de órdenes alcista", "effect_pp": 6.0, "source": "libro"}]},
    {"id": "ETH-lon", "kind": "cripto", "target": "ETH", "label": "ETH · Apertura Londres", "event": "ventana",
     "when_utc": ms(LONDON), "horizon": "1h", "prob_up": 0.35, "confidence": "alta", "drivers": []},
    {"id": "SOL-lon", "kind": "cripto", "target": "SOL", "label": "SOL · Apertura Londres", "event": "ventana",
     "when_utc": ms(LONDON), "horizon": "1h", "prob_up": 0.72, "confidence": "baja", "drivers": []},
    {"id": "XRP-lon", "kind": "cripto", "target": "XRP", "label": "XRP · Apertura Londres", "event": "ventana",
     "when_utc": ms(LONDON), "horizon": "1h", "prob_up": 0.55, "confidence": "alta", "drivers": []},
    {"id": "XRP-ny", "kind": "cripto", "target": "XRP", "label": "XRP · Apertura Nueva York", "event": "ventana",
     "when_utc": ms(datetime(2026, 10, 26, 14, 30, tzinfo=UTC)), "horizon": "1h", "prob_up": 0.9, "confidence": "alta"},
    {"id": "londres-2026-10-26", "kind": "bolsa", "target": "londres", "label": "Londres (FTSE 100)", "event": "apertura",
     "when_utc": ms(LONDON), "horizon": "sesión", "prob_up": 0.8, "confidence": "alta"}]}
BACKTEST = {"hourly_range_pct": {"BTC": 0.6, "ETH": 0.9, "SOL": 1.2, "XRP": 1.0}, "strategies": [
    {"id": "bolsa_a_cripto-tokio-btc-londres", "family": "bolsa_a_cripto", "name": "Tokio sube → BTC en Londres", "n": 400,
     "hit_rate": 0.58, "base_rate": 0.5, "lift_pp": 8.0, "avg_ret_pct": 0.1, "oos": {"hit_rate": 0.56}, "verdict": "funciona",
     "actionable": True, "market": "londres", "coin": "BTC", "window": "Apertura de Londres", "side": "long", "sharpe": 1.1},
    {"id": "malo", "family": "flujo", "name": "No funciona", "verdict": "no_funciona", "actionable": True}]}
PORTFOLIO = {"type": "portfolio", "equity": 10000, "positions": [], "pending": [], "scheduled": [],
             "limits": {"risk_per_trade_pct": 0.5}}
MARKET = {"type": "market", "prices": {"BTC": 95000.0, "SOL": 200.0}, "flow": {"BTC": {"ls": 1.4, "funding": 0.01, "pctl": 55}}}
ORDERFLOW = {"type": "orderflow", "coins": {"BTC": {"imbalance": 0.2, "cvd_15m": 8.1, "trend": "alcista", "venue": "x"}}}
TREND = {"type": "trend", "markets": {"tokio": {"state": "cerrada", "change_pct": 0.8, "last_session": {"change_pct": 0.8}}}}


class Clock:
    def __init__(self, dt):
        self.t = ms(dt)

    def __call__(self):
        return self.t


def keys_in(o) -> set:
    """Todas las claves de un esquema anidado."""
    if isinstance(o, dict):
        return set(o) | set().union(*(keys_in(v) for v in o.values()))
    if isinstance(o, list):
        return set().union(*(keys_in(v) for v in o)) if o else set()
    return set()


def drain(q):
    out = []
    while not q.empty():
        out.append(json.loads(q.get_nowait()))
    return out


@pytest.fixture
def no_creds(monkeypatch, tmp_path):
    """Sin clave ni otra credencial que pueda resolver el SDK (perfil en disco, federación)."""
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "ANTHROPIC_IDENTITY_TOKEN",
              "ANTHROPIC_IDENTITY_TOKEN_FILE", "ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "sin-config"))


def make_phase2(tmp_path, client, local=None, clock=None, submit=None, predictions=PREDICTIONS):
    store = Store(tmp_path / "t.db")
    bus = Bus(store)
    calls = []

    def default_submit(ticket, story, headline, strategy, window):
        kinds = [r[0] for r in store.db.execute("SELECT kind FROM events WHERE story=?", (story,))]
        calls.append({"ticket": ticket, "story": story, "headline": headline, "strategy": strategy, "window": window,
                      "kinds_before": kinds})
        return True

    p = ca.Phase2(bus, store, None, local, lambda: predictions, lambda: TREND, lambda: ORDERFLOW, lambda: MARKET,
                  lambda: BACKTEST, lambda: PORTFOLIO, submit or default_submit, client=client,
                  clock=clock or Clock(PRE_LONDON), range_fn=lambda c, bt, tr: ca.hourly_range_pct(c, bt, tr, load=lambda *a: []))
    return p, bus, store, calls


def seed_story(bus, clock, sid="n1", imp=7, title="SEC approves Solana ETF", age_ms=120_000):
    t = clock() - age_ms
    bus.publish({"type": "event", "ts": t, "kind": "captured", "agent": "hunter", "story": sid, "title": title,
                 "coins": ["SOL"], "text": "SEC · T0",
                 "patch": {"headline": title, "src": "SEC · prensa", "srcId": "sec", "tier": "T0", "t0": t, "lat": 4000,
                           "url": "https://www.sec.gov/x", "coins": ["SOL"], "pub": t - 4000, "region": "us"}})
    bus.publish({"type": "event", "ts": t + 30_000, "kind": "corroborated", "agent": "hunter", "story": sid, "title": title,
                 "coins": ["SOL"], "text": "Reuters",
                 "patch": {"corrob": [{"src": "Reuters", "tier": "T1", "lat": 30_000, "title": title, "url": "u", "region": "us"}]}})
    bus.publish({"type": "event", "ts": t + 60_000, "kind": "triaged", "agent": "local", "story": sid, "title": title,
                 "coins": ["SOL"], "text": "x",
                 "patch": {"triage": {"tipo": "regulatorio", "importancia": imp, "credibilidad": 90, "resumen": "La SEC aprueba el ETF de Solana",
                                      "monedas": ["SOL"], "repercusion": {"direccion": 70, "magnitud": 60, "activos": ["SOL"], "bolsas": []}}}})


# ---------------------------------------------------------------- peticiones

async def test_peticion_con_cache_salida_estructurada_y_sin_parametros_prohibidos(tmp_path):
    store = Store(tmp_path / "t.db")
    client = FakeClient(by_model())
    eng = ca.ClaudeEngine(Bus(store), store, client, Clock(PRE_LONDON))
    data, llm = await eng.ask(HAIKU, ca.VERIFY_SYSTEM, "Noticia: datos variables", ca.VERIFY_SCHEMA, "low", 4000)
    assert data == VERIFY_OK   # el texto se lee aunque la respuesta empiece por un bloque thinking
    call = client.calls[0]
    assert call["beta"] is False and call["model"] == "claude-haiku-5-5" and call["max_tokens"] == 4000
    assert call["system"] == [{"type": "text", "text": ca.VERIFY_SYSTEM, "cache_control": {"type": "ephemeral"}}]
    assert call["messages"] == [{"role": "user", "content": "Noticia: datos variables"}]   # lo variable, al final
    fmt = call["output_config"]["format"]
    assert call["output_config"]["effort"] == "low" and fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False and fmt["schema"]["required"] == ["checks", "why", "score"]
    assert not keys_in(fmt["schema"]) & {"minimum", "maximum", "maxLength", "maxItems"}   # límites solo en la descripción
    assert keys_in(ca.VERIFY_SCHEMA) >= {"minimum", "maxLength", "maxItems"}               # (la IA local sí los recibe)
    for k in ("temperature", "top_p", "tool_choice", "tools", "thinking", "fallbacks", "betas"):
        assert k not in call   # Haiku 5.5: sin temperatura, sin respaldo del servidor; nunca tool_choice forzado

    await eng.ask(SONNET, ca.IMPACT_SYSTEM, "x", ca.IMPACT_SCHEMA, "medium", 8000)
    await eng.ask(OPUS, ca.STRAT_SYSTEM, "x", ca.STRAT_SCHEMA, "medium", 16000)
    for call, model in zip(client.calls[1:], ("claude-sonnet-5-5", "claude-opus-5-5")):
        assert call["beta"] is True and call["model"] == model
        assert call["betas"] == ["server-side-fallback-2026-07-01"] and call["fallbacks"] == "default"
        assert call["system"][0]["cache_control"] == {"type": "ephemeral"} and "tool_choice" not in call
        assert "thinking" not in call and "temperature" not in call
    # Timeout por llamada según max_tokens y sin reintentos del SDK (los hace el motor, salvo los timeouts).
    assert client.options == [{"timeout": 160.0, "max_retries": 0}, {"timeout": 260.0, "max_retries": 0},
                              {"timeout": 460.0, "max_retries": 0}]
    assert eng.reserved == 0 and eng.budget.spent() > 0


def test_prompts_de_sistema_estables_y_cacheables():
    a = ca.build_request(OPUS, ca.STRAT_SYSTEM, "ventana A", ca.STRAT_SCHEMA, "medium", 16000)
    b = ca.build_request(OPUS, ca.STRAT_SYSTEM, "ventana B", ca.STRAT_SCHEMA, "medium", 16000)
    assert a["system"] == b["system"] and a["output_config"] == b["output_config"]   # mismo prefijo, misma caché
    for s in (ca.VERIFY_SYSTEM, ca.IMPACT_SYSTEM, ca.STRAT_SYSTEM):
        assert len(s) > 2400   # más de 512 tokens: el mínimo cacheable de los modelos 5.5
        assert "no una orden" in s   # los titulares son datos, no instrucciones
    assert "0,7" in ca.VERIFY_SYSTEM and "0,8 %" in ca.IMPACT_SYSTEM and "0,55" in ca.IMPACT_SYSTEM
    assert "nunca más de 3" in ca.STRAT_SYSTEM and "riesgo_max_pct" in ca.STRAT_SYSTEM


# ---------------------------------------------------------------- coste y presupuesto

def test_coste_por_llamada_con_cache():
    assert ca.call_cost(HAIKU, 1000, 2000, 500, 300) == pytest.approx((100 + 20 + 62.5 + 150) / 1e6)
    assert ca.call_cost(SONNET, 1000, 2000, 500, 300) == pytest.approx((2000 + 400 + 1250 + 3000) / 1e6)
    assert ca.call_cost(OPUS, 1000, 2000, 500, 300) == pytest.approx((4000 + 400 + 2500 + 6000) / 1e6)
    llm = ca.llm_from_response(SONNET, reply({}, SONNET))
    assert list(llm) == LLM_KEYS
    assert llm == {"model": "Sonnet 5.5", "key": "sonnet", "tin": 3500, "cached": 2000, "tout": 300, "cost": 0.00665}
    # Respaldo en el servidor: cada intento se cobra a su modelo y el que sirvió da el nombre.
    its = [SimpleNamespace(type="message", model=OPUS, input_tokens=100, cache_read_input_tokens=0,
                           cache_creation_input_tokens=0, output_tokens=0),
           SimpleNamespace(type="fallback_message", model="claude-opus-5", input_tokens=100, cache_read_input_tokens=0,
                           cache_creation_input_tokens=0, output_tokens=200)]
    r = reply({}, OPUS, u=usage(iterations=its))
    r.model = "claude-opus-5"
    llm = ca.llm_from_response(OPUS, r)
    assert llm["model"] == "Opus 5" and llm["tin"] == 200 and llm["tout"] == 200
    assert llm["cost"] == pytest.approx((100 * 4 + 100 * 5 + 200 * 25) / 1e6)


def test_presupuesto_se_reinicia_a_medianoche_de_madrid(tmp_path):
    store = Store(tmp_path / "t.db")
    clock = Clock(datetime(2026, 10, 26, 22, 59, tzinfo=UTC))   # 23:59 en Madrid (CET)
    b = ca.Budget(store, limit=1.0, clock=clock)
    b.add(0.6)
    b.add(0.5)
    assert b.spent() == pytest.approx(1.1) and b.exhausted() and b.left() == 0
    assert b.resets_at() == ms(datetime(2026, 10, 26, 23, 0, tzinfo=UTC))
    clock.t = ms(datetime(2026, 10, 26, 23, 0, tzinfo=UTC))      # 00:00 del 27 en Madrid
    assert b.day() == "2026-10-27" and b.spent() == 0 and not b.exhausted()
    clock.t = ms(datetime(2026, 10, 26, 22, 0, tzinfo=UTC))
    assert ca.Budget(store, limit=1.0, clock=clock).spent() == pytest.approx(1.1)   # sobrevive a un reinicio


async def test_presupuesto_agotado_pasa_a_local_y_avisa(tmp_path, monkeypatch):
    # El peor caso de la llamada a Haiku (4000 tokens de salida) cabe en 0,003 $; lo que cobra de verdad, no.
    monkeypatch.setattr(config, "CLAUDE_DAILY_BUDGET_USD", 0.003)
    client = FakeClient(lambda kw: reply(VERIFY_OK, kw["model"], u=usage(40000, 0, 0, 300)))
    p, bus, store, _ = make_phase2(tmp_path, client, local=FakeLocalAI())
    seed_story(bus, p.clock, "n1", imp=5)
    seed_story(bus, p.clock, "n2", imp=5)
    p.announce()
    assert bus.state["agent:verifier"]["patch"]["off"] is False
    first = (await p.on_triaged("n1"))["verify"]
    assert first["llm"]["model"] == "Haiku 5.5" and first["llm"]["cost"] > 0.003
    second = (await p.on_triaged("n2"))["verify"]
    assert second["llm"]["model"] == "Qwen local" and second["llm"]["cost"] == 0
    for a in ca.AGENTS:
        st = bus.state[f"agent:{a}"]["patch"]
        assert st["off"] is True and "Presupuesto diario agotado" in st["note"] and "medianoche de Madrid" in st["note"]


def test_modelo_servido_por_el_respaldo_se_cobra_a_su_precio():
    r = reply({}, OPUS, u=usage(1000, 0, 0, 1000))
    r.model = "claude-opus-4-8"   # el respaldo del servidor respondió con Opus 4.8 (5 / 25 $)
    llm = ca.llm_from_response(OPUS, r)
    assert llm["model"] == "Opus 4.8" and llm["cost"] == pytest.approx((1000 * 5 + 1000 * 25) / 1e6)
    r.model = "claude-opus-9"     # desconocido: precio más alto de la tabla y su id como nombre
    llm = ca.llm_from_response(OPUS, r)
    assert llm["model"] == "claude-opus-9" and llm["key"] == "opus"
    assert llm["cost"] == pytest.approx((1000 * 5 + 1000 * 25) / 1e6)
    assert ca.call_cost("claude-x", 1000, 0, 0, 0) == pytest.approx(5000 / 1e6)


def engine(tmp_path, client, limit=None):
    store = Store(tmp_path / "t.db")
    clock = Clock(PRE_LONDON)
    return ca.ClaudeEngine(Bus(store), store, client, clock, budget=ca.Budget(store, limit=limit, clock=clock))


async def test_rafaga_concurrente_no_pasa_del_presupuesto(tmp_path):
    big = usage(1000, 0, 0, 5000)   # 0,0026 $ por llamada con Haiku
    client = FakeClient(lambda kw: reply(VERIFY_OK, HAIKU, u=big), delay=0.02)
    eng = engine(tmp_path, client, limit=0.01)
    res = await asyncio.gather(*(eng.ask(HAIKU, ca.VERIFY_SYSTEM, f"noticia {i}", ca.VERIFY_SCHEMA, "low", 4000)
                                 for i in range(40)), return_exceptions=True)
    ok = [r for r in res if not isinstance(r, Exception)]
    assert all(isinstance(r, ca.BudgetExhausted) for r in res if isinstance(r, Exception))
    assert len(client.calls) == len(ok) <= 4   # antes del arreglo: las 40 llamaban a Claude
    assert eng.budget.spent() < 0.01 + 4 * 0.0026 and eng.reserved == pytest.approx(0)


async def test_rafaga_de_noticias_con_presupuesto_corto_pasa_a_local(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CLAUDE_DAILY_BUDGET_USD", 0.005)
    client = FakeClient(lambda kw: reply(VERIFY_OK, kw["model"], u=usage(1000, 0, 0, 5000)), delay=0.02)
    p, bus, store, _ = make_phase2(tmp_path, client, local=FakeLocalAI())
    for i in range(12):
        seed_story(bus, p.clock, f"n{i}", imp=5)
    outs = await asyncio.gather(*(p.on_triaged(f"n{i}") for i in range(12)))
    models = [o["verify"]["llm"]["model"] for o in outs]
    assert models.count("Haiku 5.5") == len(client.calls) <= 4 and models.count("Qwen local") == 12 - len(client.calls)
    assert p.claude.budget.spent() < 0.005 + 4 * 0.0026


async def test_timeout_no_se_reintenta_y_cuenta_en_el_presupuesto(tmp_path):
    err = anthropic.APITimeoutError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    client = FakeClient(lambda kw: err)
    p, bus, store, _ = make_phase2(tmp_path, client, local=FakeLocalAI())
    seed_story(bus, p.clock, "n1", imp=5)
    ev = (await p.on_triaged("n1"))["verify"]
    assert ev["llm"]["model"] == "Qwen local" and len(client.calls) == 1   # sin reintentos
    assert client.options == [{"timeout": 160.0, "max_retries": 0}]
    req = ca.build_request(HAIKU, ca.VERIFY_SYSTEM, client.calls[0]["messages"][0]["content"], ca.VERIFY_SCHEMA, "low", 4000)
    assert p.claude.budget.spent() == pytest.approx(ca.estimate_cost(HAIKU, req))
    assert p.claude.budget.spent() > 4000 * 0.50 / 1e6   # el peor caso: pudo generar (y cobrar) todo max_tokens
    st = bus.state["agent:verifier"]["patch"]
    assert "timeout" in st["note"] and "respaldo local" in st["note"] and st["spent"] > 0


async def test_presupuesto_cuenta_el_peor_caso_de_la_llamada(tmp_path):
    client = FakeClient(lambda kw: reply(VERIFY_OK, kw["model"]))
    eng = engine(tmp_path, client, limit=0.3)
    with pytest.raises(ca.BudgetExhausted):   # Opus con 16000 tokens de salida puede costar más de 0,32 $
        await eng.ask(OPUS, ca.STRAT_SYSTEM, "x", ca.STRAT_SCHEMA, "medium", 16000)
    assert client.calls == [] and eng.reserved == 0 and eng.budget.spent() == 0
    data, _ = await eng.ask(HAIKU, ca.VERIFY_SYSTEM, "x", ca.VERIFY_SCHEMA, "low", 4000)   # Haiku sí cabe
    assert data == VERIFY_OK and len(client.calls) == 1


async def test_errores_pasajeros_se_reintentan_y_los_demas_no(tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "RETRY_WAITS_S", (0.0, 0.0))
    errs = [APIErr(529, "overloaded"), APIErr(429, "rate limit")]
    client = FakeClient(lambda kw: errs.pop(0) if errs else reply(VERIFY_OK, kw["model"]))
    eng = engine(tmp_path, client)
    data, _ = await eng.ask(HAIKU, ca.VERIFY_SYSTEM, "x", ca.VERIFY_SCHEMA, "low", 4000)
    assert data == VERIFY_OK and len(client.calls) == 3
    bad = FakeClient(lambda kw: APIErr(400, "invalid_request_error"))
    with pytest.raises(APIErr):
        await engine(tmp_path / "b", bad).ask(HAIKU, ca.VERIFY_SYSTEM, "x", ca.VERIFY_SCHEMA, "low", 4000)
    assert len(bad.calls) == 1
    always = FakeClient(lambda kw: APIErr(503, "unavailable"))
    with pytest.raises(APIErr):
        await engine(tmp_path / "c", always).ask(HAIKU, ca.VERIFY_SYSTEM, "x", ca.VERIFY_SCHEMA, "low", 4000)
    assert len(always.calls) == 3   # dos reintentos como mucho


async def test_api_sin_respaldo_tras_rechazo_sigue_sin_el(tmp_path):
    def fn(kw):
        if "fallbacks" in kw:
            return APIErr(400, "fallbacks: Extra inputs are not permitted")
        return reply(IMPACT_BIG, kw["model"])
    client = FakeClient(fn)
    eng = engine(tmp_path, client)
    data, _ = await eng.ask(SONNET, ca.IMPACT_SYSTEM, "x", ca.IMPACT_SCHEMA, "medium", 8000)
    assert data == IMPACT_BIG and [c["beta"] for c in client.calls] == [True, False] and eng.fallbacks is False
    assert "fallbacks" not in client.calls[1] and "betas" not in client.calls[1]
    await eng.ask(OPUS, ca.STRAT_SYSTEM, "x", ca.STRAT_SCHEMA, "medium", 16000)
    assert [c["beta"] for c in client.calls] == [True, False, False]   # ya no se vuelve a intentar
    # Otro 400 (sin «fallback» en el mensaje) no apaga el respaldo: sube como error.
    other = FakeClient(lambda kw: APIErr(400, "max_tokens: too large"))
    eng2 = engine(tmp_path / "b", other)
    with pytest.raises(APIErr):
        await eng2.ask(SONNET, ca.IMPACT_SYSTEM, "x", ca.IMPACT_SCHEMA, "medium", 8000)
    assert eng2.fallbacks is True and len(other.calls) == 1


# ---------------------------------------------------------------- respaldo sin clave

async def test_sin_clave_agentes_apagados_y_todo_en_local(tmp_path, no_creds):
    assert ca.make_client() is None
    local = FakeLocalAI()
    p, bus, store, calls = make_phase2(tmp_path, None, local=local, clock=Clock(IN_LONDON))
    p.announce()
    for a in ca.AGENTS:
        assert bus.state[f"agent:{a}"]["patch"] == {"off": True, "note": "Falta la clave ANTHROPIC_API_KEY: uso el respaldo local"}
    seed_story(bus, p.clock, "n1", imp=7)
    out = await p.on_triaged("n1")
    v, im = out["verify"], out["impact"]
    assert v["kind"] == "verified" and v["llm"] == {"model": "Qwen local", "key": "local", "tin": 900, "cached": 0,
                                                    "tout": 120, "cost": 0}
    assert im["kind"] == "impact" and im["patch"]["impact"]["llm"]["model"] == "Qwen local"
    assert local.acquired == 2   # cada petición pasa por acquire (espera al hueco libre de la fábrica)
    url, body = local.s.calls[0]
    assert url == "http://127.0.0.1:8094/v1/chat/completions" and body["messages"][0]["content"] == ca.VERIFY_SYSTEM
    assert body["response_format"]["type"] == "json_schema" and body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"] == ca.VERIFY_SCHEMA   # en local, el esquema completo
    assert "SEC approves Solana ETF" in body["messages"][1]["content"]
    # El estratega pasa a reglas: tickets de las predicciones de la ventana, sin coste.
    p.clock.t = ms(PRE_LONDON)
    res = await p.check_windows(PRE_LONDON)
    assert [r["ticket"]["coin"] for r in res] == ["BTC", "ETH"] and all(r["ticket"]["llm"] is None for r in res)
    assert [c["strategy"] for c in calls] == ["reglas-prediccion-de-la-ventana"] * 2


async def test_sin_ia_local_ni_clave_no_publica_nada(tmp_path, no_creds):
    p, bus, store, _ = make_phase2(tmp_path, None, local=FakeLocalAI(ok=False))
    seed_story(bus, p.clock, "n1", imp=8)
    out = await p.on_triaged("n1")
    assert out["verify"] is None
    assert store.db.execute("SELECT COUNT(*) FROM events WHERE kind IN ('verified','unverified')").fetchone()[0] == 0


async def test_rechazo_de_claude_pasa_a_local(tmp_path):
    refusal = reply(None, HAIKU, stop="refusal", u=usage(1500, 0, 0, 0), category="cyber")
    p, bus, store, _ = make_phase2(tmp_path, FakeClient(by_model(verify=refusal)), local=FakeLocalAI())
    seed_story(bus, p.clock, "n1", imp=5)
    ev = (await p.on_triaged("n1"))["verify"]
    assert ev["llm"]["model"] == "Qwen local" and ev["kind"] == "verified"
    assert p.claude.budget.spent() == pytest.approx(1500 * 0.10 / 1e6)   # el intento rechazado también cuenta
    assert "respaldo local" in bus.state["agent:verifier"]["patch"]["note"]


# ---------------------------------------------------------------- umbrales y formato exacto

@pytest.mark.parametrize("score,kind", [(0.69, "unverified"), (0.7, "verified"), (0.95, "verified")])
async def test_umbral_de_verificacion(tmp_path, score, kind):
    p, bus, store, _ = make_phase2(tmp_path, FakeClient(by_model(verify={**VERIFY_OK, "score": score})))
    seed_story(bus, p.clock, "n1", imp=5)
    q = bus.subscribe()
    ev = (await p.on_triaged("n1"))["verify"]
    assert ev["kind"] == kind and ev["agent"] == "verifier" and ev["story"] == "n1"
    assert {"type", "kind", "agent", "story", "title", "text", "coins", "patch"} <= set(ev)
    v = ev["patch"]["verify"]
    assert set(v) == VERIFY_KEYS and v["score"] == score and isinstance(v["dt"], int)
    assert v["checks"] == VERIFY_OK["checks"] and v["why"] == VERIFY_OK["why"]
    assert list(v["llm"]) == LLM_KEYS and ev["llm"] == v["llm"]   # arriba también, para el contador de la web
    assert ev["text"].startswith(f"Veracidad {round(score * 100)} / 100 · ") and ev["coins"] == ["SOL"]
    assert any(m.get("kind") == kind for m in drain(q))


@pytest.mark.parametrize("mag,conf,kind,drop", [
    (0.79, 0.9, "noimpact", "Movimiento esperado 0,79 % por debajo del umbral de 0,8 %"),
    (1.2, 0.54, "noimpact", "Confianza 54 % por debajo del 55 % mínimo"),
    (1.2, 0.6, "impact", None)])
async def test_umbrales_de_impacto(tmp_path, mag, conf, kind, drop):
    raw = {**IMPACT_BIG, "mag": mag, "conf": conf, "per": [{"coin": "ETH", "pct": -0.5}, {"coin": "BTC", "pct": 0.4}]}
    p, bus, store, _ = make_phase2(tmp_path, FakeClient(by_model(impact=raw)))
    seed_story(bus, p.clock, "n1", imp=6)
    ev = (await p.on_triaged("n1"))["impact"]
    im = ev["patch"]["impact"]
    assert ev["kind"] == kind and ev["agent"] == "impact"
    assert set(im) == IMPACT_KEYS | ({"drop"} if drop else set()) and im.get("drop") == drop
    # Magnitudes en la dirección dir, main = mag. ETH (−0,5) iría en contra: se quita, no se le da la vuelta al signo.
    assert im["dir"] == 1 and im["main"] == "SOL" and im["per"] == {"BTC": 0.4, "SOL": mag}
    assert im["markets"] == {"nuevayork": 0.2}   # las bolsas desconocidas se descartan
    assert im["llm"]["model"] == "Sonnet 5.5" and ev["coins"] == ["BTC", "SOL"]
    if kind == "impact":
        assert ev["text"] == "Alcista · 1,2 % esperado en SOL · confianza 60 % · 1-4 h"


@pytest.mark.parametrize("imp,models", [(4, []), (5, [HAIKU]), (6, [HAIKU, SONNET])])
async def test_umbrales_de_importancia(tmp_path, imp, models):
    client = FakeClient(by_model(impact={**IMPACT_BIG, "mag": 1.0}))
    p, bus, store, _ = make_phase2(tmp_path, client)
    seed_story(bus, p.clock, "n1", imp=imp)
    await p.on_triaged("n1")
    assert [c["model"] for c in client.calls] == models
    await p.on_triaged("n1")   # la misma noticia no se procesa dos veces
    assert len(client.calls) == len(models)


async def test_sin_verificar_no_hay_impacto(tmp_path):
    client = FakeClient(by_model(verify={**VERIFY_OK, "score": 0.4}))
    p, bus, store, _ = make_phase2(tmp_path, client)
    seed_story(bus, p.clock, "n1", imp=9)
    out = await p.on_triaged("n1")
    assert out["verify"]["kind"] == "unverified" and out["impact"] is None
    assert [c["model"] for c in client.calls] == [HAIKU]


# ---------------------------------------------------------------- estratega

async def test_estratega_opus_tickets_con_limites(tmp_path):
    client = FakeClient(by_model())
    p, bus, store, calls = make_phase2(tmp_path, client)
    q = bus.subscribe()
    res = await p.check_windows(PRE_LONDON)
    assert [r["story"] for r in res] == ["w-apertura-londres-2026-10-26-btc", "w-apertura-londres-2026-10-26-eth",
                                         "w-apertura-londres-2026-10-26-sol"]   # 3 como mucho, una por moneda
    req = client.calls[-1]
    assert req["model"] == OPUS and req["system"][0]["text"] == ca.STRAT_SYSTEM
    user = req["messages"][0]["content"]
    assert "Apertura Londres" in user and "bolsa_a_cripto-tokio-btc-londres" in user and "malo" not in user
    assert '"rango_1h_pct":{"BTC":0.6' in user and '"riesgo_max_pct":0.5' in user
    btc, eth, sol = (r["ticket"] for r in res)
    for t in (btc, eth, sol):
        assert list(t) == ca.STRAT_KEYS and t["timing"] == "window" and t["win"] == "Apertura Londres"
        assert t["winAt"] == ms(LONDON) + 5 * 60_000 and t["entryType"] == ca.ENTRY_WINDOW
        assert t["valid"] == "Apertura Londres, 09:00 Madrid" and t["risk"] <= config.RISK_PER_TRADE_PCT
        assert t["llm"]["model"] == "Opus 5.5"
    assert btc["risk"] == config.RISK_PER_TRADE_PCT and btc["stopPct"] == 0.9 and btc["trailPct"] == 0.8
    assert eth["side"] == "short" and eth["trailPct"] == eth["stopPct"] == 1.2   # sin trailing → igual al stop
    assert sol["mode"] == "Predicción de la ventana" and sol["tp2R"] >= sol["tp1R"]
    # strategy publicado antes de entregar cada ticket, con la estrategia y la ventana.
    assert [c["kinds_before"] for c in calls] == [["strategy"]] * 3
    assert [c["strategy"] for c in calls] == ["seguir-bolsa-anterior", "contra-el-minorista-extremo", "prediccion-de-la-ventana"]
    assert calls[0]["window"] == "Apertura Londres" and calls[0]["headline"] == "Apertura Londres · Compra BTC · Seguir bolsa anterior"
    evs = [m for m in drain(q) if m["type"] == "event"]
    assert [e["kind"] for e in evs] == ["strategy"] * 3 and [("llm" in e) for e in evs] == [True, False, False]
    assert set(evs[0]["patch"]) == {"headline", "src", "tier", "t0", "coins", "strat"} and evs[0]["agent"] == "strat"
    assert evs[0]["text"].startswith("Programada para Apertura Londres (09:00 Madrid)")
    # No se repite la ventana, ni tras un reinicio.
    assert await p.check_windows(PRE_LONDON) == [] and len(client.calls) == 1
    p2, *_ = make_phase2(tmp_path, client)
    assert await p2.check_windows(PRE_LONDON) == [] and len(client.calls) == 1
    assert p.view()["planes"][0]["motor"] == "Opus 5.5"


async def test_estratega_sin_ventaja_publica_nostrategy(tmp_path):
    client = FakeClient(by_model(strat={"why": "Señales mezcladas", "tickets": []}))
    p, bus, store, calls = make_phase2(tmp_path, client)
    assert await p.check_windows(PRE_LONDON) == [] and calls == []
    body = store.db.execute("SELECT body FROM events WHERE kind='nostrategy'").fetchone()[0]
    ev = json.loads(body)
    assert ev["story"] == "w-apertura-londres-2026-10-26" and ev["text"] == "Señales mezcladas"
    assert ev["patch"]["strat"] == {"none": True, "why": "Señales mezcladas", "llm": ev["llm"]}


def test_reglas_desde_las_predicciones():
    ctx = {"predictions": PREDICTIONS, "ranges": {"BTC": 0.6, "ETH": 0.9}, "timing": "window",
           "limits": {"riesgo_max_pct": 0.5, "max_tickets": 3}}
    btc, eth = ca.RulesStrategist().plan("Apertura Londres", LONDON, ctx)
    # SOL (confianza baja), XRP (55 %) y la predicción de otra ventana se quedan fuera.
    assert (btc["coin"], btc["side"], eth["coin"], eth["side"]) == ("BTC", "long", "ETH", "short")
    assert btc["stopPct"] == btc["trailPct"] == 0.9 and eth["stopPct"] == eth["trailPct"] == 1.35
    assert list(btc) == ca.STRAT_KEYS and btc["llm"] is None and btc["mode"] == "Predicción de la ventana"
    assert btc["risk"] == 0.5 and btc["winAt"] == ms(LONDON) + 5 * 60_000 and "65 %" in btc["why"]
    one = ca.RulesStrategist().plan("Apertura Londres", LONDON, {**ctx, "limits": {"riesgo_max_pct": 0.3, "max_tickets": 1}})
    assert [t["coin"] for t in one] == ["BTC"] and one[0]["risk"] == 0.3   # la más clara primero
    [none] = ca.RulesStrategist().plan("Apertura Tokio", LONDON.replace(hour=0), {**ctx, "predictions": {"items": []}})
    assert none["none"] is True and none["llm"] is None and "60 %" in none["why"]


def test_prediccion_de_la_proxima_hora_no_cuenta_como_de_la_ventana():
    # Con la forma de predict.py, 15 min antes de Apertura Londres: por moneda, la de la ventana y la de la próxima hora.
    def item(coin, pid, label, when, horizon, prob, conf):
        return {"id": pid, "kind": "cripto", "target": coin, "label": label, "event": "ventana", "when_utc": when,
                "horizon": horizon, "prob_up": prob, "confidence": conf, "drivers": []}
    now = ms(PRE_LONDON)
    items = [item("BTC", "BTC-londres-2026-10-26", "BTC · Apertura Londres", ms(LONDON), "4h", 0.64, "media"),
             item("BTC", "BTC-1h-2026-10-26T07:45", "BTC · próxima hora", now, "1h", 0.30, "media"),
             item("ETH", "ETH-londres-2026-10-26", "ETH · Apertura Londres", ms(LONDON), "4h", 0.66, "media"),
             item("ETH", "ETH-1h-2026-10-26T07:45", "ETH · próxima hora", now, "1h", 0.20, "baja")]
    pred = {"items": items}
    got = ca.window_predictions(pred, "Apertura Londres", LONDON)
    assert sorted(p["id"] for p in got) == ["BTC-londres-2026-10-26", "ETH-londres-2026-10-26"]
    ctx = {"predictions": pred, "ranges": {}, "timing": "window", "limits": {"riesgo_max_pct": 0.5, "max_tickets": 3}}
    plan = {t["coin"]: t for t in ca.RulesStrategist().plan("Apertura Londres", LONDON, ctx)}
    assert plan["BTC"]["side"] == "long" and "64 %" in plan["BTC"]["why"]   # antes: corto con «30 % de subida»
    assert plan["ETH"]["side"] == "long" and "66 %" in plan["ETH"]["why"]   # antes: se perdía
    brief = ca.predictions_brief(pred, "Apertura Londres", LONDON)
    assert [i["de_esta_ventana"] for i in brief["items"]] == [True, False, True, False]
    # Una predicción a 10 min del inicio no es de la ventana; sin hora, vale que la etiqueta la nombre.
    off = {**items[0], "id": "BTC-x", "label": "BTC · otra", "when_utc": ms(LONDON) + 10 * 60_000}
    assert ca.window_predictions({"items": [off]}, "Apertura Londres", LONDON) == []
    no_hour = {**items[0], "when_utc": None}
    assert ca.window_predictions({"items": [no_hour]}, "Apertura Londres", LONDON) == [no_hour]


IMPACT_SOL = {"dir": 1, "mag": 2.1, "conf": 0.7, "hz": "1-4 h", "main": "SOL", "per": {"SOL": 2.1}, "why": "x"}


async def test_noticias_simultaneas_no_pasan_de_3_tickets_por_ventana(tmp_path):
    one = {"why": "x", "tickets": [STRAT_4["tickets"][3]]}   # Opus propone un SOL largo cada vez
    client = FakeClient(by_model(strat=one), delay=0.05)
    p, bus, store, calls = make_phase2(tmp_path, client, clock=Clock(IN_LONDON))
    key = ("Apertura Londres", ms(LONDON))
    p._win_count[key] = 2   # la ventana ya lleva 2 de 3
    for i in range(3):
        seed_story(bus, p.clock, f"n{i}", imp=8)
    outs = await asyncio.gather(*(p.plan_news(p.story(f"n{i}"), IMPACT_SOL, "Apertura Londres", LONDON)
                                  for i in range(3)))
    assert p._win_count[key] == 3 and len(calls) == 1 and sorted(len(o) for o in outs) == [0, 0, 1]
    assert [c["model"] for c in client.calls] == [OPUS]   # las otras dos ven la ventana llena sin llamar a Opus
    whys = [json.loads(b)["text"] for (b,) in store.db.execute("SELECT body FROM events WHERE kind='nostrategy'")]
    assert len(whys) == 2 and all("no quedan huecos" in w for w in whys)


async def test_noticia_vieja_no_abre_entrada_inmediata(tmp_path):
    client = FakeClient(by_model())
    p, bus, store, calls = make_phase2(tmp_path, client, clock=Clock(IN_LONDON))
    seed_story(bus, p.clock, "n1", imp=8, age_ms=40 * 60_000)   # captada hace 40 min (cola en los agentes)
    out = await p.on_triaged("n1")
    assert out["impact"]["kind"] == "impact" and out["plan"] == [] and calls == []
    assert [c["model"] for c in client.calls] == [HAIKU, SONNET]   # Opus no llega a llamarse
    ev = json.loads(store.db.execute("SELECT body FROM events WHERE kind='nostrategy'").fetchone()[0])
    assert ev["story"] == "n1" and set(ev["patch"]) == {"strat"} and ev["patch"]["strat"]["none"] is True
    assert ev["text"].startswith("Noticia de hace 40 min") and "llm" not in ev


async def test_noticia_que_caduca_mientras_opus_piensa_no_entra(tmp_path):
    clock = Clock(IN_LONDON)

    def fn(kw):
        clock.t += 14 * 60_000   # Opus tarda: la noticia, de hace 2 min, pasa de los 15
        return reply(STRAT_4, kw["model"])
    p, bus, store, calls = make_phase2(tmp_path, FakeClient(fn), clock=clock)
    seed_story(bus, clock, "n1", imp=8)
    assert await p.plan_news(p.story("n1"), IMPACT_SOL, "Apertura Londres", LONDON) == [] and calls == []
    ev = json.loads(store.db.execute("SELECT body FROM events WHERE kind='nostrategy'").fetchone()[0])
    assert ev["story"] == "n1" and ev["text"].startswith("Noticia de hace 16 min")
    assert ev["llm"]["model"] == "Opus 5.5"   # el gasto de Opus sigue a la vista


async def test_ventana_sin_predicciones_espera_a_que_lleguen(tmp_path):
    def plans(store):
        return store.db.execute("SELECT COUNT(*) FROM events WHERE kind IN ('strategy','nostrategy')").fetchone()[0]
    client = FakeClient(by_model())
    p, bus, store, calls = make_phase2(tmp_path, client, predictions=None)
    # Recién arrancado, 14 min antes de Londres: el Predictor aún no ha hecho su primera ronda.
    assert await p.check_windows(PRE_LONDON) == [] and client.calls == [] and plans(store) == 0
    p.get_predictions = lambda: PREDICTIONS   # llega la ronda: ahora sí se prepara
    assert len(await p.check_windows(datetime(2026, 10, 26, 7, 47, tzinfo=UTC))) == 3
    # Si no llegan nunca, a 3 min se prepara igual, una sola vez.
    client2 = FakeClient(by_model(strat={"why": "Sin datos de la ventana", "tickets": []}))
    p2, bus2, store2, _ = make_phase2(tmp_path / "b", client2, predictions=None)
    assert await p2.check_windows(datetime(2026, 10, 26, 7, 56, tzinfo=UTC)) == [] and plans(store2) == 0
    await p2.check_windows(datetime(2026, 10, 26, 7, 57, tzinfo=UTC))
    await p2.check_windows(datetime(2026, 10, 26, 7, 57, 30, tzinfo=UTC))
    assert plans(store2) == 1 and len(client2.calls) == 1


async def test_ventana_que_falla_se_reintenta_pocas_veces(tmp_path, monkeypatch):
    p, bus, store, calls = make_phase2(tmp_path, FakeClient(by_model()))
    real, fails = p.recent_news, []

    def flaky(*a, **k):
        if not fails:
            fails.append(1)
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **k)
    monkeypatch.setattr(p, "recent_news", flaky)
    assert await p.check_windows(PRE_LONDON) == []
    assert "No pude preparar Apertura Londres" in bus.state["agent:strat"]["patch"]["now"]
    assert len(await p.check_windows(PRE_LONDON)) == 3   # la vuelta siguiente la prepara entera
    assert await p.check_windows(PRE_LONDON) == []       # y ya no se repite
    p2, *_ = make_phase2(tmp_path / "b", FakeClient(by_model()))

    def broken(*a, **k):
        raise RuntimeError("roto")
    monkeypatch.setattr(p2, "recent_news", broken)
    for _ in range(ca.PLAN_RETRIES + 2):
        assert await p2.check_windows(PRE_LONDON) == []
    assert p2._plan_fails[("Apertura Londres", ms(LONDON))] == ca.PLAN_RETRIES   # se rinde tras 3 intentos


async def test_impacto_grande_con_ventana_abierta_entra_ya(tmp_path):
    strat = {"why": "x", "tickets": [STRAT_4["tickets"][3], STRAT_4["tickets"][0]]}
    client = FakeClient(by_model(strat=strat))
    p, bus, store, calls = make_phase2(tmp_path, client, clock=Clock(IN_LONDON))
    seed_story(bus, p.clock, "n1", imp=8)
    out = await p.on_triaged("n1")
    assert out["impact"]["patch"]["impact"]["mag"] == 2.1
    [c] = calls   # un solo ticket, en el expediente de la noticia
    t = c["ticket"]
    assert c["story"] == "n1" and c["window"] == "Apertura Londres" and c["headline"] == "SEC approves Solana ETF"
    assert t["timing"] == "now" and t["winAt"] is None and t["entryType"] == ca.ENTRY_NOW and t["valid"] == "60 min"
    assert t["coin"] == "SOL" and list(t) == ca.STRAT_KEYS
    assert "disparador" in client.calls[-1]["messages"][0]["content"]
    ev = json.loads(store.db.execute("SELECT body FROM events WHERE kind='strategy'").fetchone()[0])
    assert set(ev["patch"]) == {"strat"} and ev["text"] == "Entrada ya · stop 1,6 % · trailing 1,4 % · riesgo 0,4 %"


async def test_impacto_menor_o_sin_ventana_no_despierta_al_estratega(tmp_path):
    client = FakeClient(by_model(impact={**IMPACT_BIG, "mag": 1.5}))
    p, bus, store, calls = make_phase2(tmp_path, client, clock=Clock(IN_LONDON))
    seed_story(bus, p.clock, "n1", imp=8)
    assert (await p.on_triaged("n1"))["plan"] is None
    client2 = FakeClient(by_model())
    p2, bus2, *_ = make_phase2(tmp_path / "b", client2, clock=Clock(datetime(2026, 10, 26, 10, 0, tzinfo=UTC)))
    seed_story(bus2, p2.clock, "n1", imp=8)
    assert (await p2.on_triaged("n1"))["plan"] is None and [c["model"] for c in client2.calls] == [HAIKU, SONNET]


# ---------------------------------------------------------------- bucles

async def test_bucles_de_noticias_y_ventanas_y_cierre(tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "TRIAGE_POLL_S", 0.01)
    monkeypatch.setattr(ca, "WINDOW_POLL_S", 0.01)
    client = FakeClient(by_model())
    p, bus, store, calls = make_phase2(tmp_path, client)   # 14 min antes de Londres: toca prepararla
    task = asyncio.create_task(p.run())
    seed_story(bus, p.clock, "n1", imp=5)

    def verified():
        return store.db.execute("SELECT COUNT(*) FROM events WHERE kind='verified' AND story='n1'").fetchone()[0]
    for _ in range(300):
        await asyncio.sleep(0.01)
        if verified() and len(calls) == 3:
            break
    await asyncio.sleep(0.05)   # unas vueltas más: ni la noticia ni la ventana se repiten
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await p.close()
    assert verified() == 1 and len(calls) == 3 and client.closed
    assert sorted(c["model"] for c in client.calls) == [HAIKU, OPUS]
    assert bus.state["agent:strat"]["patch"]["now"] == "Apertura Londres: 3 tickets (Opus 5.5)"


async def test_vigilancia_desactivada_solo_prepara_ventanas(tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "TRIAGE_POLL_S", 0.01)
    monkeypatch.setattr(ca, "WINDOW_POLL_S", 0.01)
    client = FakeClient(by_model())
    p, bus, store, calls = make_phase2(tmp_path, client, clock=Clock(IN_LONDON))   # sin ventanas que preparar
    task = asyncio.create_task(p.run(watch_triage=False))
    seed_story(bus, p.clock, "n1", imp=5)
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await p.close()
    assert client.calls == [] and client.closed


# ---------------------------------------------------------------- utilidades

def test_ventanas_y_rango_horario():
    assert ca.window_story("Datos macro EE.UU.", LONDON) == "w-datos-macro-ee-uu-2026-10-26"
    assert ca.window_story("Apertura Tokio", datetime(2026, 10, 25, 23, 30, tzinfo=UTC)) == "w-apertura-tokio-2026-10-26"
    assert ca.open_window(IN_LONDON) == ("Apertura Londres", LONDON)
    assert ca.open_window(datetime(2026, 10, 26, 9, 5, tzinfo=UTC)) is None
    assert ca.open_window(datetime(2026, 10, 26, 12, 45, tzinfo=UTC))[0] == "Datos macro EE.UU."
    rows = [[0, 100, 101, 99, 100, 0]] * 24   # rango del 2 % por vela
    assert ca.hourly_range_pct("BTC", {"hourly_range_pct": {"BTC": 0.7}}) == 0.7
    assert ca.hourly_range_pct("ETH", {}, {"hourly_range_pct": {"ETH": 1.1}}) == 1.1
    assert ca.hourly_range_pct("SOL", None, None, load=lambda c, i: rows) == pytest.approx(2.0)
    assert ca.hourly_range_pct("XRP", None, None, load=lambda c, i: []) == ca.DEFAULT_RANGE_PCT["XRP"]


# ---------------------------------------------------------------- fuentes vivas y perpetuos

def test_cortos_de_la_fase_2_van_en_perpetuo():
    args = ("Predicción de la ventana", 0.5, 2.0, None, 1.5, 3.0, "inval", "why", "now", "Apertura Londres", LONDON,
            None, 0.5)
    assert ca.make_ticket("SOL", "short", *args)["instrument"] == "perp"
    assert ca.make_ticket("SOL", "long", *args)["instrument"] == "spot"


async def test_fuente_silenciada_no_gasta_ia_de_pago_ni_entra_en_el_contexto(tmp_path):
    client = FakeClient(by_model())
    p, bus, store, _ = make_phase2(tmp_path, client)
    seed_story(bus, p.clock, "n1", imp=9)
    seed_story(bus, p.clock, "n2", imp=9, title="SEC approves XRP ETF")
    t = p.clock() - 119_000   # la captada de n1 venía de una fuente silenciada (fuentes_vivas)
    bus.publish({"type": "event", "ts": t, "kind": "captured", "agent": "hunter", "story": "n1", "title": "x",
                 "patch": {"srcState": "silenciada", "srcWeight": 0.0}})
    assert ca.muted(p.story("n1")) and not ca.muted(p.story("n2")) and ca.muted({"srcWeight": 0})
    out = await p.on_triaged("n1")
    assert out == {"verify": None, "impact": None, "plan": None} and client.calls == []
    await p.on_triaged("n2")
    assert [c["model"] for c in client.calls][:1] == [HAIKU]
    assert [n["resumen"] for n in p.recent_news()] == ["La SEC aprueba el ETF de Solana"]   # solo n2 (verificada)


def test_contexto_del_estratega_ve_la_subida_de_estado_y_pondera_por_fuente_y_medio(tmp_path, monkeypatch):
    """Phase2.recent_news: una historia de una fuente silenciada que luego confirma una activa («corroborated» con
    srcState) o que trae un feed mejor («srcstate») cuenta con su estado nuevo; cada noticia pesa srcWeight × el peso
    aprendido de su medio y se ordena por importancia × peso."""
    from bot import aprendizaje
    monkeypatch.setattr(aprendizaje, "_PESOS", {"sec.gov": 1.2})
    monkeypatch.setattr(aprendizaje, "_ALIAS", {})
    p, bus, store, _ = make_phase2(tmp_path, FakeClient(by_model()))
    for sid, title in (("n1", "SEC approves Solana ETF"), ("n2", "SEC approves XRP ETF"), ("n3", "SEC approves ADA ETF"),
                       ("n4", "SEC approves DOT ETF")):
        seed_story(bus, p.clock, sid, imp=8, title=title)
    t = p.clock() - 100_000
    for sid in ("n1", "n2", "n3"):   # las tres llegaron por una fuente silenciada
        bus.publish({"type": "event", "ts": t, "kind": "captured", "agent": "hunter", "story": sid, "title": "x",
                     "patch": {"srcState": "silenciada", "srcWeight": 0.0}})
    bus.publish({"type": "event", "ts": t + 1000, "kind": "corroborated", "agent": "hunter", "story": "n1", "title": "x",
                 "patch": {"corrob": [{"src": "Reuters", "srcWeight": 1.0}], "srcState": "activa", "srcWeight": 1.0}})
    bus.publish({"type": "event", "ts": t + 2000, "kind": "srcstate", "agent": "hunter", "story": "n2", "title": "x",
                 "patch": {"srcState": "prueba", "srcWeight": 0.5}})
    bus.publish({"type": "event", "ts": t + 3000, "kind": "corroborated", "agent": "hunter", "story": "n3", "title": "x",
                 "patch": {"corrob": [{"src": "Blog", "srcWeight": 0.0}]}})   # confirmación sin subida: sigue silenciada
    news = p.recent_news()
    assert [n["peso"] for n in news] == [1.2, 1.2, 0.6]   # n1 y n4 activas (×1,2 del medio); n2 en prueba; n3 fuera
