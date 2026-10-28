"""La web (dashboard/radar.html): su JavaScript se ejecuta con Node sobre un DOM mínimo de prueba.

Cada prueba arranca la página (en directo con un WebSocket falso, o la simulación), le pasa mensajes como los del bot
y mira lo que pinta o lo que envía al bot. Sin Node, se saltan."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = Path(__file__).resolve().parents[1] / "dashboard" / "radar.html"
_NODE_M = Path(r"M:\OpenClaw-runtime\node\node.exe")
NODE = os.environ.get("NODE") or (str(_NODE_M) if _NODE_M.exists() else shutil.which("node"))
pytestmark = pytest.mark.skipif(not NODE, reason="hace falta Node para probar la web")

# Arnés: extrae el <script> de la página, lo ejecuta en un contexto vm con un DOM falso y luego el escenario (stdin).
HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const [htmlPath, mode] = process.argv.slice(1);
const html = fs.readFileSync(htmlPath, "utf8");
const js = [...html.matchAll(/<script(?![^>]*\bsrc=)([^>]*)>([\s\S]*?)<\/script>/g)]
  .filter(m => !/json|text\/plain/.test(m[1])).map(m => m[2]).join("\n;\n");
const errors = [], sockets = [], fetches = [], timers = [], timeouts = [];
const H = {errors, sockets, fetches, timers, timeouts, confirmAnswer: true, fetchReply: null, perf: 1000};
function makeEl(sel){
  const el = {_sel: sel, innerHTML: "", textContent: "", hidden: false, className: "", title: "", value: "", dataset: {},
    _attrs: {}, _listeners: {}, offsetHeight: 0, offsetWidth: 0, scrollWidth: 0,
    style: {setProperty(){}, getPropertyValue(){ return ""; }},
    classList: {_s: new Set(), add(c){ this._s.add(c); }, remove(c){ this._s.delete(c); }, toggle(c, on){ on ? this._s.add(c) : this._s.delete(c); }, contains(c){ return this._s.has(c); }},
    setAttribute(k, v){ this._attrs[k] = String(v); }, getAttribute(k){ return k in this._attrs ? this._attrs[k] : null; },
    hasAttribute(k){ return k in this._attrs; }, removeAttribute(k){ delete this._attrs[k]; },
    addEventListener(t, f){ (this._listeners[t] = this._listeners[t] || []).push(f); },
    getContext(){ return new Proxy({}, {get: () => () => {}}); },
    getBoundingClientRect(){ return {left: 0, top: 0, width: 0, height: 0}; },
    closest(){ return null; }, matches(){ return false; }, querySelector(){ return makeEl("?"); },
    scrollIntoView(){}, focus(){}, appendChild(){}, setPointerCapture(){}};
  return el;
}
const els = new Map(), $q = sel => { if (!els.has(sel)) els.set(sel, makeEl(sel)); return els.get(sel); };
const docL = {};
const document = {querySelector: $q, querySelectorAll: () => [], getElementById: () => null,
  addEventListener(t, f){ (docL[t] = docL[t] || []).push(f); },
  createElement: () => makeEl("nuevo"), documentElement: makeEl("html"), body: makeEl("body"), head: makeEl("head"),
  fullscreenElement: null, activeElement: null, contains: () => true, exitFullscreen(){}};
class FakeWS { constructor(url){ this.url = url; this.readyState = 0; this.sent = []; sockets.push(this); }
  send(d){ this.sent.push(JSON.parse(d)); } close(){ this.readyState = 3; if (this.onclose) this.onclose(); } }
// Destino falso de un evento: closest() reconoce selectores de atributo ([data-x] o [data-x="v"]) separados por comas.
function target(attrs){
  const t = {dataset: {}, hasAttribute: k => k in attrs, getAttribute: k => (k in attrs ? attrs[k] : null),
    closest(sel){ return sel.split(",").some(s => { const m = s.trim().match(/^\[([\w-]+)(?:="(.*)")?\]$/);
      return m && (m[1] in attrs) && (m[2] === undefined || m[2] === attrs[m[1]]); }) ? t : null; },
    matches(sel){ return !!t.closest(sel); }};
  for (const [k, v] of Object.entries(attrs)) if (k.startsWith("data-")) t.dataset[k.slice(5).replace(/-(\w)/g, (_, c) => c.toUpperCase())] = v;
  return t;
}
const ctx = {
  console: {log(){}, info(){}, warn(){}, debug(){}, error: (...a) => errors.push(a.map(x => (x && x.stack) || String(x)).join(" "))},
  document, location: {href: "http://127.0.0.1:8765/", hash: "", host: "127.0.0.1:8765", protocol: "http:"},
  history: {replaceState(){}}, localStorage: {getItem: () => null, setItem(){}}, navigator: {},
  innerWidth: 1400, innerHeight: 900, devicePixelRatio: 1,
  setInterval: (f, ms) => { timers.push({f, ms}); return timers.length; }, clearInterval(){},
  setTimeout: (f, ms) => { timeouts.push({f, ms}); return timeouts.length; }, clearTimeout(){},
  requestAnimationFrame: () => 0, cancelAnimationFrame(){},
  performance: {now: () => H.perf},
  getComputedStyle: () => ({display: "block", flexDirection: "row", getPropertyValue: () => ""}),
  ResizeObserver: class { observe(){} }, CSS: {escape: s => String(s)},
  WebSocket: FakeWS, confirm: () => H.confirmAnswer, addEventListener(){},
  fetch: async url => { fetches.push(url); return H.fetchReply ? H.fetchReply(url) : {ok: false}; }};
ctx.window = ctx;
if (mode === "live") ctx.RADAR_LIVE = {ws: "/events"};
vm.createContext(ctx);
let result = null;
(async () => {
  try {
    vm.runInContext(js, ctx, {filename: "radar.js"});
    Object.assign(H, {el: $q, target,
      ws: () => sockets[sockets.length - 1],
      open(){ const w = H.ws(); w.readyState = 1; if (w.onopen) w.onopen(); return w; },
      recv(...msgs){ H.ws().onmessage({data: JSON.stringify(msgs.length === 1 ? msgs[0] : msgs)}); },
      sent: () => (H.ws() ? H.ws().sent : []),
      fire(type, attrs, extra = {}){ (docL[type] || []).forEach(f => f({target: target(attrs), button: 0, isPrimary: true, ...extra})); },
      elFire(sel, type, ev){ ($q(sel)._listeners[type] || []).forEach(f => f(ev)); }});
    const code = fs.readFileSync(0, "utf8");
    result = await vm.runInContext(`(async function(H){\n${code}\n})`, ctx)(H);
  } catch (e) { errors.push("excepción: " + ((e && e.stack) || e)); }
  process.stdout.write(JSON.stringify({result, errors}) + "\n");
})();
"""

# Ayudas comunes de los escenarios en directo: eventos y cartera con el formato del bot (contrato 2.5 y 2.6).
LIVE = r"""
const now = Date.now();
const ev = (kind, story, patch, extra = {}) => ({type: "event", kind, story, title: kind, text: "", coins: [], patch, ts: Date.now(), ...extra});
const strat = (coin = "BTC", side = "long", o = {}) => ({coin, side, mode: "seguir-bolsa", risk: 0.5, stopPct: 1.2, trailPct: 0.8,
  timing: "now", win: "Apertura Londres", winAt: null, entryType: "Mercado", tp1R: 1.5, tp2R: null, valid: "60 min",
  inval: "pierde el VWAP", why: "Prueba", llm: null, ...o});
const pf = (o = {}) => ({type: "portfolio", ts: now, equity: 10000, start_equity: 10000, day_pnl: 0, day_pnl_pct: 0,
  positions: [], pending: [], scheduled: [], closed: [],
  limits: {risk_per_trade_pct: 0.5, max_daily_loss_pct: 3, max_positions: 4, day_loss_pct: 0, halted: false, halt_until: null},
  mode: "veto", ...o});
const reset = () => H.recv({type: "reset", phase: 2, started: now - 3600e3});
"""


def run(code: str, live: bool = True):
    p = subprocess.run([NODE, "-e", HARNESS, str(HTML), "live" if live else "sim"],
                       input=(LIVE if live else "") + code, capture_output=True, text=True, encoding="utf-8", timeout=180)
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert out["errors"] == [], out["errors"]
    return out["result"]


def test_en_directo_se_ven_el_modo_y_la_parada():
    """#13 #20 #49: en directo solo se ocultan los mandos de la simulación; el selector de modo se ve y la parada (paper),
    solo cuando la cartera del bot dice que la admite (caps o «halted» en la raíz; limits.halted es el límite diario)."""
    r = run("""H.open(); reset();
      const kill0 = H.el("#killBtn").hidden;
      H.recv(pf()); const kill1 = H.el("#killBtn").hidden;
      H.recv(pf({halted: false})); const kill2 = H.el("#killBtn").hidden;
      H.recv(pf({caps: ["cancel"]})); const kill3 = H.el("#killBtn").hidden;
      H.recv(pf({caps: ["halt", "cancel"]}));
      return {speed: H.el("#speedSeg").hidden, ctrl: H.el(".top .ctrl").hidden, sel: H.el("#approvalSel").hidden,
              kill0, kill1, kill2, kill3, kill: H.el("#killBtn").hidden, killTxt: H.el("#killBtn").textContent,
              tag: H.el("#modeTag").textContent, note: H.el("#tradesNote").hidden};""")
    assert r["speed"] is True and r["ctrl"] is False and r["sel"] is False
    assert r["kill0"] is True and r["kill1"] is True and r["kill2"] is False and r["kill3"] is True and r["kill"] is False
    assert r["killTxt"] == "Detener paper"
    assert "paper" in r["tag"] and r["note"] is False


def test_sin_soporte_del_bot_no_se_ofrece_parar_ni_cancelar():
    """Revisión de #13/#20/#49: si el bot no declara halt ni cancel, la web no los ofrece ni envía, y no da por hecho nada."""
    r = run("""H.open(); reset();
      const at = now + 3600e3;
      H.recv(ev("strategy", "n2", {headline: "h", src: "s", tier: "T1", t0: now, strat: strat()}),
             ev("riskok", "n2", {risk: {checks: [], ok: true}}),
             ev("humanok", "n2", {human: {mode: "auto", by: "Automático (paper)", at: now}}),
             ev("scheduled", "n2", {exec: {scheduled: at}}));
      H.recv(pf({scheduled: [{story: "n2", at, expires: at + 600e3, coin: "BTC", side: "long"}]}));
      S.sel = "n2"; renderTrace(); const tr0 = H.el("#trace").innerHTML;
      userHalt(); const t1 = H.el("#toast").textContent;
      H.fire("click", {"data-cancel": "n2"}); const t2 = H.el("#toast").textContent;
      const sent0 = H.sent().length;
      H.recv(pf({caps: ["halt", "cancel"], scheduled: [{story: "n2", at, expires: at + 600e3, coin: "BTC", side: "long"}]}));
      renderTrace();
      return {tr0, t1, t2, sent0, tr1: H.el("#trace").innerHTML, stop: S.execStop};""")
    assert "Programada para" in r["tr0"] and "data-cancel" not in r["tr0"]
    assert "aún no admite" in r["t1"] and "aún no admite" in r["t2"] and r["sent0"] == 0 and r["stop"] is False
    assert 'data-cancel="n2"' in r["tr1"]


def test_parada_y_cancelacion_sin_confirmar_avisan():
    """Con soporte declarado, el aviso es «pedido, pendiente de confirmar»; si la cartera no lo confirma, se avisa."""
    r = run("""H.open(); reset();
      const at = now + 3600e3, sch = [{story: "n2", at, expires: at + 600e3, coin: "BTC", side: "long"}];
      H.recv(pf({caps: ["halt", "cancel"], scheduled: sch}));
      H.elFire("#killBtn", "click", {}); const t1 = H.el("#toast").textContent;
      H.timeouts.filter(x => x.ms === 10000).forEach(x => x.f()); const t2 = H.el("#toast").textContent;
      H.timeouts.length = 0;
      H.fire("click", {"data-cancel": "n2"}); const t3 = H.el("#toast").textContent;
      H.timeouts.filter(x => x.ms === 10000).forEach(x => x.f()); const t4 = H.el("#toast").textContent;
      H.timeouts.length = 0;
      H.fire("click", {"data-cancel": "n2"});
      H.recv(pf({caps: ["halt", "cancel"], scheduled: []})); H.el("#toast").textContent = "";
      H.timeouts.filter(x => x.ms === 10000).forEach(x => x.f());
      return {t1, t2, t3, t4, t5: H.el("#toast").textContent, sent: H.sent()};""")
    assert "Pendiente de que la confirme" in r["t1"] and "enviada" not in r["t1"]
    assert "no ha confirmado la parada" in r["t2"]
    assert "Pendiente de que la confirme" in r["t3"] and "no ha confirmado la cancelación" in r["t4"]
    assert r["t5"] == ""
    assert r["sent"] == [{"type": "halt"}, {"type": "cancel", "story": "n2"}, {"type": "cancel", "story": "n2"}]


def test_sin_conexion_avisa_y_no_pierde_el_modo():
    """#14: sin WebSocket abierto, aprobar avisa con un toast y el selector vuelve al modo real del bot."""
    r = run("""reset(); H.recv(pf({mode: "veto"}));
      userDecide("n1", true);
      const t1 = H.el("#toast").textContent, s1 = H.sent().length;
      const sel = H.el("#approvalSel"); sel.value = "manual"; H.elFire("#approvalSel", "change", {target: sel});
      const back = sel.value;
      H.open(); userDecide("n1", false);
      return {t1, s1, back, approval: S.approval, sent: H.sent()};""")
    assert "Sin conexión" in r["t1"] and r["s1"] == 0
    assert r["back"] == "veto" and r["approval"] == "veto"
    assert r["sent"] == [{"type": "decision", "story": "n1", "ok": False}]


def test_aprobar_va_en_click_y_no_se_redibuja_mientras_se_pulsa():
    """#15: pulsar no decide nada; el clic sí. Mientras el botón está pulsado no se redibuja el expediente."""
    r = run("""H.open(); reset();
      H.recv(ev("strategy", "n1", {headline: "Titular", src: "X", tier: "T1", t0: now, strat: strat()}),
             ev("riskok", "n1", {risk: {checks: [{ok: true, t: "ok"}], ok: true}}),
             ev("humanwait", "n1", {human: {mode: "manual", deadline: now + 600000}}));
      H.recv(pf({pending: [{story: "n1", deadline: now + 600000, mode: "manual", coin: "BTC", side: "long"}], mode: "manual"}));
      S.sel = "n1"; render(true);
      const hasBtn = H.el("#trace").innerHTML.includes('data-approve="n1"');
      H.fire("pointerdown", {"data-approve": "n1"});
      const afterDown = H.sent().length;
      H.el("#trace").innerHTML = "SENTINEL"; render(true); const held = H.el("#trace").innerHTML;
      H.fire("pointerup", {"data-approve": "n1"}); H.fire("click", {"data-approve": "n1"});
      return {hasBtn, afterDown, held, sent: H.sent(), redrawn: H.el("#trace").innerHTML !== "SENTINEL"};""")
    assert r["hasBtn"] and r["afterDown"] == 0 and r["held"] == "SENTINEL"
    assert r["sent"] == [{"type": "decision", "story": "n1", "ok": True}] and r["redrawn"]


def test_mandos_en_directo_envian_al_bot():
    """#20: modo, detener/reanudar (confirmado por la cartera), cancelar y cerrar salen por el WebSocket."""
    r = run("""H.open(); reset(); H.recv(pf({mode: "veto", caps: ["halt", "cancel"]}));
      const sel = H.el("#approvalSel"); sel.value = "manual"; H.elFire("#approvalSel", "change", {target: sel});
      H.elFire("#killBtn", "click", {});
      const k1 = H.el("#killBtn").textContent;
      H.recv(pf({mode: "manual", halted: true, caps: ["halt", "cancel"]}));
      const k2 = H.el("#killBtn").textContent, st = agentState("exec").txt;
      H.elFire("#killBtn", "click", {});
      H.fire("click", {"data-cancel": "n2"});
      H.fire("click", {"data-close": "n3"});
      H.confirmAnswer = false; H.fire("click", {"data-close": "n4"});
      return {sent: H.sent(), k1, k2, st};""")
    assert r["sent"] == [{"type": "mode", "mode": "manual"}, {"type": "halt"}, {"type": "resume"},
                         {"type": "cancel", "story": "n2"}, {"type": "close", "story": "n3"}]
    assert r["k1"] == "Detener paper" and r["k2"] == "Reanudar paper" and r["st"] == "Detenido por ti"


def test_expediente_de_planes_w_y_a():
    """#10: los planes de ventana (w-) y del motor de aperturas (a-) muestran estrategia, riesgo y aprobación."""
    r = run("""H.open(); reset();
      const id = "w-1761400000000-BTC";
      H.recv(ev("strategy", id, {headline: "Plan Apertura Londres: compra BTC", src: "Estratega · Reglas", tier: "Plan", t0: now, coins: ["BTC"], strat: strat()}),
             ev("riskok", id, {risk: {checks: [{ok: true, t: "Riesgo 0,5 %"}], ok: true}}),
             ev("humanwait", id, {human: {mode: "manual", deadline: now + 600000}}));
      H.recv(pf({pending: [{story: id, deadline: now + 600000, mode: "manual", coin: "BTC", side: "long"}], mode: "manual"}));
      S.sel = id; renderTrace(); const a = H.el("#trace").innerHTML;
      const id2 = "a-londres-ETH-1";
      H.recv(ev("strategy", id2, {headline: "Motor: venta ETH", src: "Motor de aperturas", tier: "Plan", t0: now, coins: ["ETH"], strat: strat("ETH", "short")}));
      S.sel = id2; renderTrace();
      return {a, b: H.el("#trace").innerHTML, id};""")
    a, b = r["a"], r["b"]
    assert "Estrategia propuesta" in a and f'data-approve="{r["id"]}"' in a and "Plan en curso" in a
    assert "Triaje con IA local" not in a and "Control de riesgo" in a
    assert "Motor de aperturas" in b and "Estrategia propuesta" in b


def test_sin_titular_no_rompe_las_pestanas():
    """#11: una estrategia cuyo «captured» quedó fuera del historial (sin titular) no rompe Estrategias ni el expediente."""
    r = run("""H.open(); reset();
      H.recv(ev("strategy", "n9", {strat: strat("SOL", "short")}), ev("riskok", "n9", {risk: {checks: [], ok: true}}),
             ev("humanwait", "n9", {human: {mode: "veto", deadline: now + 30000}}));
      H.recv(pf({pending: [{story: "n9", deadline: now + 30000, mode: "veto"}]}));
      renderTrades(); const k = H.el("#kanban").innerHTML;
      S.sel = "n9"; renderTrace(); renderMando();
      return {k, tr: H.el("#trace").innerHTML, s: short(undefined)};""")
    assert "Venta SOL" in r["k"]
    assert "Estrategia propuesta" in r["tr"] and 'data-reject="n9"' in r["tr"]
    assert r["s"] == ""


def test_capital_en_directo_no_suma_dos_veces_lo_latente():
    """#12 #21: el capital del bot ya va a precio de mercado; la base es start_equity, no 10.000 fijo."""
    r = run("""H.open(); reset();
      H.recv({type: "market", prices: {BTC: 110}});
      H.recv(pf({equity: 10099, positions: [{story: "n1", coin: "BTC", side: "long", qty: 10, entry: 100, sl: 95,
                 riskUsd: 50, notional: 1000, upnl: 99, headline: "x"}]}));
      const eq = equityNow(); renderTrades(); const k = H.el("#kpis").innerHTML;
      H.recv(pf({equity: 5100, start_equity: 5000})); renderTrades();
      return {eq, k, k2: H.el("#kpis").innerHTML, base: eqBase()};""")
    assert r["eq"] == 10099
    assert "10.099,00" in r["k"] and "+99,00" in r["k"]
    assert r["base"] == 5000 and "+100,00" in r["k2"] and "+2,00 %" in r["k2"]


def test_balanza_no_usa_noticias_archivadas_por_impacto():
    """#16: si el analista de impacto la archivó, su triaje no inclina la balanza."""
    r = run("""H.open(); reset();
      const tr = {tipo: "macro", importancia: 7, credibilidad: 80, sentimiento: "alcista",
                  repercusion: {direccion: 60, magnitud: 70, activos: ["BTC"]}, monedas: ["BTC"]};
      H.recv(ev("captured", "n1", {headline: "h", src: "s", tier: "T1", t0: now, coins: ["BTC"]}), ev("triaged", "n1", {triage: tr}),
             ev("verified", "n1", {verify: {score: 0.9, checks: []}}),
             ev("noimpact", "n1", {impact: {dir: -1, mag: 0.3, conf: 0.6, drop: "Movimiento pequeño", per: {BTC: 0.3}}}));
      H.recv(ev("captured", "n2", {headline: "h2", src: "s", tier: "T1", t0: now, coins: ["BTC"]}), ev("triaged", "n2", {triage: tr}));
      const b = newsTilt(S.stories.get("n2"), Date.now());
      return {a: newsTilt(S.stories.get("n1"), Date.now()), b: b && b.dir};""")
    assert r["a"] is None and r["b"] == 1


def test_apertura_armada_no_es_buen_momento():
    """#17: «armada» espera la ruptura: no es buen momento. «en_posicion» del motor sin posición en el paper (pendiente
    de aprobar, programada o recién enviada) tampoco: solo una posición real del paper lo es."""
    r = run("""H.open(); reset();
      const sig = () => coinSignal("BTC", newsBalance(Date.now()), Date.now());
      H.recv({type: "openings", windows: [{name: "Apertura Londres", state: "armada", coins: {BTC: {state: "armada", signal: 1, why: "Rango medido"}}}]});
      const a = sig().mom;
      H.recv({type: "openings", windows: [{name: "Apertura Londres", state: "en_posicion", coins: {BTC: {state: "en_posicion", signal: 1, entries: 1,
              plan: {stopPct: 1.2, tp2R: 2, rr: 2, ok: true, why: "R:R 2,00"}}}}]});
      H.recv(pf({mode: "manual", pending: [{story: "a-europa-BTC-1", deadline: now + 600000, mode: "manual", coin: "BTC", side: "long"}]}));
      const b = sig();
      H.recv(pf({halted: true})); const c = sig();
      H.recv(pf({halted: false, positions: [{story: "a-europa-BTC-1", coin: "BTC", side: "long", qty: 1, entry: 100, sl: 98.8,
                 riskUsd: 12, notional: 100, upnl: 6, headline: "x"}]}));
      return {a, b, c, d: sig()};""")
    assert r["a"] == "wait"
    assert r["b"]["mom"] == "wait" and r["b"]["momTxt"] == "Entrada enviada al paper, pendiente: stop 1,20 % · R:R 2,00"
    assert r["c"]["mom"] == "no" and r["c"]["momTxt"] == "Paper detenido: la entrada no se ejecutará"
    assert r["d"]["mom"] == "ok" and r["d"]["momTxt"].startswith("En posición paper (compra)")
    for s in (r["b"], r["c"]):
        assert "En posición" not in s["momTxt"]


def test_bloqueada_muestra_el_motivo_del_bot():
    """#18: el expediente usa exec.why del bot, no «has detenido la ejecución»."""
    html = run("""H.open(); reset();
      H.recv(ev("strategy", "n1", {headline: "h", src: "s", tier: "T1", t0: now, strat: strat()}),
             ev("riskok", "n1", {risk: {checks: [], ok: true}}),
             ev("humanok", "n1", {human: {mode: "auto", by: "Automático (paper)", at: now}}),
             ev("blocked", "n1", {exec: {blocked: true, why: "Máximo de 4 posiciones abiertas"}}));
      S.sel = "n1"; renderTrace(); return H.el("#trace").innerHTML;""")
    assert "No enviada: Máximo de 4 posiciones abiertas." in html and "has detenido" not in html


def test_mando_refresca_resultados_y_usa_la_cartera():
    """#19: con Mando abierto se recargan los resultados (cada 30 s y al cerrar una operación); capital y hoy, de la cartera."""
    r = run("""H.open(); reset();
      H.recv(pf({equity: 10250.5, day_pnl: 250, day_pnl_pct: 2.5}));
      H.fetches.length = 0;
      S.tab = "mando"; H.timers.find(x => x.ms === 30000).f();
      const byTimer = H.fetches.slice(); H.fetches.length = 0;
      H.recv(pf({equity: 10250.5, closed: [{story: "n1", closed: now, pnl: 1}]}));
      const byClose = H.fetches.slice();
      H.recv(pf({equity: 10250.5, day_pnl: 250, day_pnl_pct: 2.5}));
      renderMando();
      return {byTimer, byClose, book: H.el("#mdBook").innerHTML};""")
    assert "/api/resultados" in r["byTimer"] and "/api/resultados" in r["byClose"]
    assert "10.250,50" in r["book"] and "+2,50 %" in r["book"]


def test_historias_con_tope():
    """#22: S.stories no crece sin límite y conserva la elegida y los planes vivos."""
    r = run("""H.open(); reset();
      H.recv(ev("strategy", "plan-vivo", {headline: "plan", src: "s", tier: "Plan", t0: now, strat: strat()}));
      S.sel = "n0";
      const evs = []; for (let i = 0; i < 2600; i++) evs.push(ev("captured", "n" + i, {headline: "h" + i, src: "s", tier: "T2", t0: now}));
      H.recv(...evs);
      return {size: S.stories.size, sel: S.stories.has("n0"), plan: S.stories.has("plan-vivo"), last: S.stories.has("n2599")};""")
    assert r["size"] <= 2000 and r["sel"] and r["plan"] and r["last"]


def test_backtest_vacio_no_rompe():
    """#43: un backtest vacío cuenta como no cargado y se avisa en vez de romper la pestaña."""
    r = run("""H.fetchReply = url => url === "/api/backtest" ? {ok: true, json: async () => ({strategies: [], leadlag: {}})} : {ok: false};
      H.open(); reset(); await loadApi("bt");
      S.tab = "bt"; S.btSig = null; renderAll(true);
      return {none: S.backtest === undefined, rows: H.el("#btRows").innerHTML};""")
    assert r["none"] and "aún no ha generado" in r["rows"]


def test_aprobaciones_automaticas_no_son_tuyas():
    """#51: el embudo y la tarjeta «Tú» separan las aprobaciones automáticas de las tuyas."""
    r = run("""H.open(); reset();
      H.recv(ev("humanok", "n1", {human: {mode: "auto", by: "Automático (paper)", at: now}}),
             ev("humanok", "n2", {human: {mode: "manual", by: "Aprobada por ti", at: now}}));
      renderFunnel(); return {f: H.el("#funnel").innerHTML, auto: S.counts.humanauto};""")
    assert r["auto"] == 1 and "1 por ti · 1 auto" in r["f"]


def test_flujo_sin_datos_no_es_cero():
    """#45: volumen, compras, CVD y rentabilidades nulos se muestran como «sin datos» o «–», no como 0."""
    r = run("""H.open(); reset();
      H.recv({type: "orderflow", coins: {BTC: {trend: null, buy_ratio_5m: null, vol_ratio: null, vol_5m_usd: null, imbalance: 0.1,
              cvd_5m: null, cvd_15m: null, ret_15m_pct: null, ret_60m_pct: null}}});
      const s = coinSignal("BTC", newsBalance(Date.now()), Date.now()); renderWorldMarkets();
      return {why: s.why.join(" | "), of: H.el("#ofTiles").innerHTML};""")
    assert "volumen sin datos" in r["why"] and "×0,0" not in r["why"] and "×0,0" not in r["of"]
    for k in ("CVD 5 min", "CVD 15 min", "15 min", "60 min"):
        assert f"<small>{k}</small><b>–</b>" in r["of"], k
    assert "0,00" not in r["of"]


def test_expediente_sin_objetivo_final_ni_promesas():
    """#41 #42: sin tp2 no se pinta 0,0000000; importancia 4 no promete verificación."""
    r = run("""H.open(); reset();
      H.recv(ev("strategy", "n1", {headline: "h", src: "s", tier: "T1", t0: now, strat: strat()}),
             ev("riskok", "n1", {risk: {checks: [], ok: true}}),
             ev("humanok", "n1", {human: {mode: "auto", by: "Automático (paper)", at: now}}),
             ev("filled", "n1", {exec: {px: 100, slip: 1, ms: 200, notional: 1000, sl: 98, tp1: 103, tp2: null, tp1Frac: 0}}));
      S.sel = "n1"; renderTrace(); const a = H.el("#trace").innerHTML;
      H.recv(ev("captured", "n2", {headline: "h2", src: "s", tier: "T2", t0: now}),
             ev("triaged", "n2", {triage: {tipo: "otro", importancia: 4, temas: [], monedas: []}}));
      S.sel = "n2"; renderTrace();
      return {a, b: H.el("#trace").innerHTML};""")
    assert "0,0000000" not in r["a"] and "sin tope: stop dinámico" in r["a"] and "a mercado, al momento" in r["a"]
    assert "No se verifica" in r["b"] and "sin verificar" in r["b"]


def test_simulacion_pinta_todas_las_pestanas():
    """La demostración (sin bot) arranca y pinta todas las pestañas sin errores; cerrar una posición funciona."""
    r = run("""for (const t of ["live", "world", "mando", "pred", "trades", "results", "bt", "market", "evo", "ai"]) H.fire("click", {"data-tab": t});
      S.tab = "world"; renderAll(true);
      const p = S.positions[0]; if (p) userClose(p.id);
      H.elFire("#killBtn", "click", {});
      return {closed: p ? !S.positions.includes(p) : true, kill: H.el("#killBtn").textContent, stop: S.execStop, sent: H.sockets.length};""", live=False)
    assert r["closed"] and r["kill"] == "Reanudar ejecución" and r["stop"] is True and r["sent"] == 0


# Mensaje «openings» del motor de aperturas v2 (bot/apertura.py, build_status): fuente de los parámetros, aviso, cupo del
# día y, por moneda, puerta de probabilidades, bandera MOVING y plan.
OPENINGS_V2 = r"""
const coin = (o = {}) => ({signal: 1, strength: 0.6, why: "Señal verde", drivers: [], range: {hi: 101, lo: 99}, rvol: 1.8,
  buy_ratio: 0.58, entries: 0, results: [], state: "armada", last_why: "Sin ruptura: cierre dentro del rango",
  gate: null, moving: null, plan: null, ...o});
const params = {range_min: 30, rvol_min: 1.5, trail_atr: 0, min_strength: 0.2, confirm_tf: 5, stop_mode: "or_mid",
  target_r: 2, target_pct: null, tp1_frac: 0, be_r: null, trail_start_r: null, min_rr: 1, moving_pct: 3,
  max_day_entries: 3, max_day_risk_pct: 1.5, gate: false, gate_pp: 5, gate_min_n: 20};
const openings = (coins, o = {}) => ({type: "openings", ts: now, params_source: "preset_v2", params,
  notice: "No hay recomendación del backtest (falta aperturas.json <b>): se usa el preset V2 sin puerta",
  day: {date: "2026-10-26", entries: 1, risk_pct: 0.5, max_entries: 3, max_risk_pct: 1.5},
  windows: [{id: "europa-2026-10-26", name: "Europa", markets: ["frankfurt", "londres", "madrid"], open_utc: now,
             state: "armada", why: "", coins}], ...o});
const MOVED = {moving: true, pct: 3.4, hours: 4,
  why: "El movimiento ya ha ocurrido: +3,40 % en las 4 h previas (≥ 3,0 %): no se entra"};
"""


def test_motor_aperturas_v2_pinta_fuente_aviso_cupo_y_comprobaciones():
    """Panel «Motor de aperturas»: fuente de los parámetros, aviso escapado, cupo del día y, por moneda, el veredicto de
    la puerta en texto claro, la bandera de movimiento ya hecho y el plan (stop, R:R) con su porqué."""
    html = run(OPENINGS_V2 + """H.open(); reset();
      H.recv(openings({
        BTC: coin({state: "en_posicion", entries: 1,
                   gate: {verdict: "claramente_a_favor", n: 30, decided: 25, edge_pp: 26.7, avg_R: 0.4, ok: true, dir: 1,
                          text: "Claramente a favor: objetivo antes que stop en 60 % de 25 rupturas pasadas → puerta superada"},
                   moving: {moving: false, pct: 1.2, hours: 4, why: "+1,20 % en las 4 h previas (< 3,0 %)"},
                   plan: {stopPct: 1.2, tp2R: 2, rr: 2, ok: true, why: "R:R 2,00 desde 100: objetivo 102,4 (+2,40 %), stop 1,20 %"}}),
        ETH: coin({state: "terminada", last_why: MOVED.why, moving: MOVED}),
        SOL: coin({state: "terminada", gate: {verdict: "pocos_casos", n: 5, decided: 4, edge_pp: null, avg_R: -0.1, ok: false,
                                              text: "Pocos casos (4 de 20 como mínimo): la puerta no se cumple"}}),
        XRP: coin({signal: -1, plan: {stopPct: 1, tp2R: 0.8, rr: 0.8, ok: false,
                                      why: "R:R 0,80 < 1,00 desde la entrada real 0,5: el movimiento ya ha ocurrido"}})}));
      renderOpenings(); return H.el("#openings").innerHTML;""")
    assert "Preset V2" in html and "del preset V2" in html
    assert "falta aperturas.json &lt;b&gt;" in html and "aperturas.json <b>" not in html
    assert "26/10" in html and "1 de 3 entradas" in html and "riesgo 0,50 % de 1,50 % del capital" in html
    for t in ("rango 30 min", "confirmación al cierre de 5 min", "stop en el medio del rango", "objetivo 2,0 R",
              "R:R ≥ 1,0", "sin stop dinámico", "ya se movió ≥ 3,0 %", "puerta de probabilidades apagada"):
        assert t in html, t
    assert "Puerta: claramente a favor → se entra" in html and "puerta superada" in html
    assert "Sin movimiento previo: +1,2 % en 4 h" in html
    assert '<span class="pill good">Último plan: stop 1,20 % · R:R 2,00</span>' in html and "objetivo 102,4" in html
    assert "Movimiento ya hecho: +3,4 % en 4 h" in html and "(≥ 3,0 %): no se entra" in html
    assert "Puerta: pocos casos → no se entra" in html and "4 de 20 como mínimo" in html
    assert "Plan rechazado: stop 1,00 % · R:R 0,80" in html and "R:R 0,80 &lt; 1,00" in html
    for bad in ("undefined", "null", "NaN"):
        assert bad not in html, bad


def test_motor_aperturas_tolera_campos_ausentes_o_nulos():
    """Sin campos v2, con null o con objetos a medias, el panel y la señal del Mundo no rompen ni pintan «null»."""
    r = run("""H.open(); reset();
      H.recv({type: "openings", windows: [], day: null, notice: null, params_source: null, params: null});
      renderOpenings(); const a = H.el("#openings").innerHTML;
      H.recv({type: "openings", params_source: "backtest", notice: null, params: {gate: true},
              day: {date: null, entries: 0, risk_pct: null, max_entries: null, max_risk_pct: null},
              windows: [null, {name: "Tokio y Seúl", state: "armada", coins: {BTC: null, ETH: {signal: 1},
                SOL: {state: "midiendo_rango", gate: {}, moving: {moving: false, pct: null}, plan: {ok: false}}}},
                {name: "Sin monedas", coins: null}]});
      renderOpenings(); const b = H.el("#openings").innerHTML;
      const sig = c => coinSignal(c, newsBalance(Date.now()), Date.now());
      const s = sig("SOL"), e = sig("ETH"), x = sig("BTC");
      H.recv({type: "openings", windows: "rota"}); renderOpenings();
      return {a, b, s, e, x, c: H.el("#openings").innerHTML, z: sig("BTC")};""")
    assert "aún no ha publicado" in r["a"] and "Parámetros" not in r["a"]
    b = r["b"]
    assert "Backtest" in b and "elegidos por el backtest" in b
    assert "0 entradas (sin tope)" in b and "riesgo 0,00 % (sin tope) del capital" in b and "(hoy," in b
    assert "puerta de probabilidades (≥ 20 casos y ventaja ≥ 5 pp)" in b
    assert "Puerta: sin veredicto → no se entra" in b and "Movimiento previo sin comprobar" in b
    assert "Plan rechazado: R:R sin calcular" in b and "Tokio y Seúl" in b
    for html in (r["a"], b, r["c"]):
        for bad in ("undefined", "null", "NaN"):
            assert bad not in html, bad
    assert r["s"]["mom"] == "wait" and r["s"]["momTxt"] == "Esperar: el plan no da el R:R mínimo"
    assert r["e"]["mom"] == "wait" and r["e"]["momTxt"] == "Tokio y Seúl: armada"
    assert "aún no ha publicado" in r["c"] and r["z"]["mom"] in ("ok", "no", "wait")


def test_mundo_usa_moving_puerta_y_plan_del_motor():
    """Vista Mundo: con estado del motor para la moneda, su bandera MOVING, su puerta y su plan deciden el «momento»."""
    r = run(OPENINGS_V2 + """H.open(); reset();
      H.recv(openings({
        BTC: coin({state: "terminada", last_why: MOVED.why, moving: MOVED}),
        ETH: coin({state: "terminada", signal: -1, last_why: "Puerta de probabilidades: Pocos casos",
                   gate: {verdict: "pocos_casos", n: 5, decided: 4, ok: false, text: "Pocos casos (4 de 20 como mínimo)"}}),
        SOL: coin({plan: {stopPct: 1, rr: 0.8, ok: false, why: "R:R 0,80 < 1,00 desde la entrada real 150: el movimiento ya ha ocurrido"}}),
        XRP: coin({state: "en_posicion", entries: 1, moving: {moving: false, pct: 0.5, hours: 4, why: "+0,50 %"},
                   gate: {verdict: "a_favor", n: 30, decided: 22, edge_pp: 7.5, avg_R: 0.12, ok: true},
                   plan: {stopPct: 1.2, tp2R: 2, rr: 2, ok: true, why: "R:R 2,00 desde 0,5"}})}));
      const sig = c => coinSignal(c, newsBalance(Date.now()), Date.now());
      const r = Object.fromEntries(["BTC", "ETH", "SOL", "XRP"].map(c => [c, sig(c)]));
      H.fire("click", {"data-tab": "world"}); S.tab = "world"; renderAll(true);
      return {r, left: H.el("#wLeft").innerHTML};""")
    s = r["r"]
    assert s["BTC"]["mom"] == "no" and s["BTC"]["act"] == "buy"
    assert s["BTC"]["momTxt"] == "No es momento: el movimiento ya ha ocurrido (+3,4 % en 4 h)"
    assert "Antes de la apertura: +3,4 % en 4 h (movimiento ya hecho)" in s["BTC"]["why"]
    assert s["ETH"]["mom"] == "no" and s["ETH"]["act"] == "sell"
    assert s["ETH"]["momTxt"] == "No es momento: la puerta de probabilidades no se cumple (pocos casos)"
    assert s["SOL"]["mom"] == "wait" and s["SOL"]["momTxt"].startswith("Esperar: R:R 0,80 < 1,00")
    assert "Plan del motor: stop 1,00 % · R:R 0,80 (rechazado)" in s["SOL"]["why"]
    # el motor dice «en_posicion» pero el paper aún no tiene la posición: pendiente, no «en posición»
    assert s["XRP"]["mom"] == "wait" and s["XRP"]["momTxt"] == "Entrada enviada al paper, pendiente: stop 1,20 % · R:R 2,00"
    assert "Plan del motor: stop 1,20 % · R:R 2,00" in s["XRP"]["why"]
    assert ("Puerta de probabilidades: se inclina a favor (superada) · 22 rupturas decididas de 30 · ventaja +7,5 pp"
            " · R medio +0,12") in s["XRP"]["why"]
    left = r["left"]
    assert "No es momento: el movimiento ya ha ocurrido (+3,4 % en 4 h)" in left
    assert "Esperar: R:R 0,80 &lt; 1,00" in left and "R:R 0,80 < 1,00" not in left


def test_mundo_ventana_terminada_por_moving_sigue_mandando():
    """Caso real del motor: con una sola moneda con señal, MOVING la acaba en la primera vela y la ventana sale
    «terminada»; la siguiente ventana está «esperando». El Mundo sigue enseñando por qué no es momento durante la
    sesión (4 h) y no salta a «Se calcula 15 min antes de la apertura». Con otra ventana ya armada para la moneda,
    manda esa; pasada la sesión, vuelve a la siguiente."""
    r = run(OPENINGS_V2 + """H.open(); reset();
      const quiet = (st, why = "") => ({signal: 0, strength: 0, why, drivers: [], range: null, rvol: null, buy_ratio: null,
        entries: 0, results: [], state: st, last_why: why, gate: null, moving: null, plan: null});
      const wait = quiet("esperando", "Se calcula 15 min antes de la apertura");
      const win = (id, name, open_utc, state, coins) => ({id, name, markets: [], open_utc, state, why: "", coins});
      const ny = win("nuevayork", "Nueva York", now + 5 * 3600e3, "esperando", {BTC: wait, ETH: wait, SOL: wait, XRP: wait});
      const europa = open => win("europa", "Europa", open, "terminada",
        {BTC: coin({state: "terminada", last_why: MOVED.why, moving: MOVED}), ETH: quiet("sin_senal"), SOL: quiet("sin_senal"), XRP: quiet("sin_senal")});
      const sig = c => coinSignal(c, newsBalance(Date.now()), Date.now());
      H.recv(openings(null, {windows: [europa(now - 10 * 60e3), ny]}));
      const a = sig("BTC"), e = sig("ETH");
      H.fire("click", {"data-tab": "world"}); S.tab = "world"; renderAll(true); const left = H.el("#wLeft").innerHTML;
      const china = win("china", "Shanghái y Hong Kong", now + 15 * 60e3, "armada",
        {BTC: coin({state: "armada", last_why: "Armada: se mide el rango en la apertura"}), ETH: quiet("sin_senal"), SOL: quiet("sin_senal"), XRP: quiet("sin_senal")});
      H.recv(openings(null, {windows: [europa(now - 75 * 60e3), china]})); const b = sig("BTC");
      H.recv(openings(null, {windows: [europa(now - 5 * 3600e3), ny]})); const c = sig("BTC");
      return {a, e, b, c, left};""")
    a = r["a"]
    assert a["mom"] == "no" and a["act"] == "buy"
    assert a["momTxt"] == "No es momento: el movimiento ya ha ocurrido (+3,4 % en 4 h)"
    assert "Antes de la apertura: +3,4 % en 4 h (movimiento ya hecho)" in a["why"]
    assert "No es momento: el movimiento ya ha ocurrido" in r["left"]
    assert r["e"]["mom"] == "wait" and r["e"]["momTxt"] == "Se calcula 15 min antes de la apertura"
    assert r["b"]["mom"] == "wait" and r["b"]["momTxt"] == "Armada: se mide el rango en la apertura"
    assert r["c"]["mom"] == "wait" and r["c"]["momTxt"] == "Se calcula 15 min antes de la apertura"


def test_plan_sin_ejecutar_no_sale_en_verde_y_rr_sin_calcular():
    """El motor guarda el plan antes del tope diario y del envío: si no llegó a operarse, la pastilla es neutra y dice
    por qué. Un plan rechazado con R:R nulo (el precio ya cruzó el stop) no es «sin objetivo fijo» si hay objetivo."""
    r = run(OPENINGS_V2 + """H.open(); reset();
      const tope = "Tope diario de 3 entradas alcanzado (todas las ventanas, día de Madrid)";
      const cruzado = {stopPct: 0.95, tp2R: null, rr: null, ok: false, why: "El precio (99,1) ya ha cruzado el stop (99,2): la ruptura ha fallado"};
      H.recv(openings({
        BTC: coin({state: "terminada", entries: 0, last_why: tope, plan: {stopPct: 1.2, tp2R: 2, rr: 2, ok: true, why: "R:R 2,00 desde 100"}}),
        ETH: coin({plan: cruzado}),
        SOL: coin({state: "terminada", entries: 1, last_why: "Salida (stop, -1,3 %): máximo de 0 reentradas",
                   plan: {stopPct: 1, tp2R: 2, rr: 2, ok: true, why: "R:R 2,00 desde 150"}})}));
      renderOpenings(); const html = H.el("#openings").innerHTML;
      const sig = c => coinSignal(c, newsBalance(Date.now()), Date.now());
      const btc = sig("BTC"), eth = sig("ETH");
      const sinObj = {...openings({}), params: {...params, target_r: null, target_pct: null}};
      H.recv({...sinObj, windows: [{name: "Europa", state: "armada", open_utc: now, coins: {XRP: coin({state: "en_posicion", entries: 1,
              plan: {stopPct: 1, tp2R: null, rr: null, ok: true, why: "Sin objetivo fijo · stop 1,00 %"}})}}]});
      renderOpenings();
      return {html, btc, eth, html2: H.el("#openings").innerHTML};""")
    html = r["html"]
    assert '<span class="pill muted">Último plan sin ejecutar: stop 1,20 % · R:R 2,00</span>' in html
    assert "R:R 2,00 desde 100 · Tope diario de 3 entradas alcanzado" in html
    assert '<span class="pill good">Último plan: stop 1,00 % · R:R 2,00</span>' in html
    assert "Plan rechazado: stop 0,95 % · R:R sin calcular" in html and "sin objetivo fijo</span>" not in html
    assert "Plan del motor: stop 1,20 % · R:R 2,00 (sin ejecutar)" in r["btc"]["why"]
    assert "Plan del motor: stop 0,95 % · R:R sin calcular (rechazado)" in r["eth"]["why"]
    assert "Último plan: stop 1,00 % · sin objetivo fijo" in r["html2"]


def test_motor_aperturas_tolera_hora_rota_y_moving_sin_horas():
    """Una apertura con hora no numérica no revienta el panel; MOVING sin «hours» no se inventa las 4 h."""
    r = run(OPENINGS_V2 + """H.open(); reset();
      H.recv({...openings({BTC: coin({state: "terminada", moving: {moving: true, pct: 3.4, why: "Movimiento ya hecho"}})}),
              windows: [{name: "Rota", state: "armada", open_utc: "x", coins: {BTC: coin({state: "terminada", moving: {moving: true, pct: 3.4}})}},
                        {name: "Enorme", state: "armada", open_utc: 1e300, coins: {ETH: coin()}}]});
      renderOpenings();
      return {html: H.el("#openings").innerHTML, s: coinSignal("BTC", newsBalance(Date.now()), Date.now())};""")
    assert "Rota" in r["html"] and "Enorme" in r["html"] and "Invalid" not in r["html"]
    assert "Movimiento ya hecho: +3,4 %</span>" in r["html"] and " h</span>" not in r["html"]
    assert r["s"]["momTxt"] == "No es momento: el movimiento ya ha ocurrido (+3,4 %)"


# Mensaje «vigilancia» (bot/vigilancia.py, build_status): operaciones VIRTUALES de las estrategias congeladas del
# laboratorio frente a su backtest fuera de muestra.
VIGIL = r"""
const E1 = {id: "btc:ma20|rvol:1,5|todos|ninguno|asia|SOL|todos|pb:15|3|2|sig", codigo: "E1", clase: "prometedora",
  origen: "congelada", nombre: "Tendencia diaria de BTC: a favor de BTC frente a su media de 20 días",
  reglas: {sl_pct: 3, r: 2, tp_pct: 6, dur: "sig", dur_texto: "hasta la misma apertura del día siguiente"},
  vivo: {operaciones: 0, abiertas: 0, R_medio: null, R_total: 0, ganadoras: null, pct_medio: null, R_medio_modelo: null,
         largos: 0, cortos: 0, por_motivo: {objetivo: 0, stop: 0, tiempo: 0}, ic95: null},
  backtest: {operaciones: 34, R_medio: 0.2048, ganadoras: 0.5588, periodo: "2025-08-16 a 2026-10-23"}, abiertas: [], ultimas: []};
const E2 = {id: "ap_mom:15:0,2|rvol:1,5|todos|a_favor|todas|todas|todos|t:30|3|3|sig", codigo: "E2", clase: "prometedora",
  origen: "congelada", nombre: "Movimiento de los primeros minutos <script>",
  reglas: {sl_pct: 3, r: 3, tp_pct: 9, dur: "sig", dur_texto: "hasta la misma apertura del día siguiente"},
  vivo: {operaciones: 6, abiertas: 1, R_medio: 0.512, R_total: 3.07, ganadoras: 0.5, pct_medio: 1.5, R_medio_modelo: 0.5,
         largos: 4, cortos: 2, por_motivo: {objetivo: 3, stop: 3, tiempo: 0}, ic95: [-1.1, 2.12]},
  backtest: {operaciones: 278, R_medio: 0.1402, ganadoras: 0.518, periodo: "2025-08-16 a 2026-10-23"},
  abiertas: [{moneda: "XRP", lado: "largo", apertura: "nuevayork-2026-10-27", ventana: "Nueva York",
              entrada_t: Date.UTC(2026, 9, 27, 14, 0), entrada: 1.5203, stop: 1.4747, objetivo: 1.6571, R_latente: -0.12}],
  ultimas: [{moneda: "SOL", lado: "corto", apertura: "europa-2026-10-27", ventana: "Europa", entrada_t: Date.UTC(2026, 9, 27, 8, 30),
             salida_t: Date.UTC(2026, 9, 27, 12, 0), R: 2.95, pct: 8.85, motivo: "objetivo"}]};
const vig = (o = {}) => ({type: "vigilancia", ts: now, estado: "ok", nota: null,
  aviso: "Operaciones VIRTUALES: no se compra ni se vende nada <b>", costes: "Costes del laboratorio.",
  desde: Date.UTC(2026, 9, 26, 23, 0), desde_dia: "2026-10-27", congelada: "2026-10-26", datos_hasta: Date.UTC(2026, 9, 27, 18, 0),
  evaluacion: "Cada apertura se evalúa unas 4 h después.", aperturas_evaluadas: 3,
  aperturas: [{key: "nuevayork-2026-10-27", nombre: "Nueva York", open_ms: Date.UTC(2026, 9, 27, 13, 30), monedas: ["BTC"],
               entradas: [{estrategia: E2.id, codigo: "E2", moneda: "XRP", lado: "largo", motivo: "abierta", R: null}]},
              {key: "europa-2026-10-27", nombre: "Europa", open_ms: Date.UTC(2026, 9, 27, 8, 0), monedas: ["BTC"], entradas: []}],
  proxima: {key: "tokio-seul-2026-10-28", nombre: "Tokio y Seúl", open_ms: Date.UTC(2026, 9, 28, 0, 0), evalua_ms: Date.UTC(2026, 9, 28, 4, 1)},
  estrategias: [E1, E2], ...o});
"""


def test_vigilancia_en_vivo_frente_al_backtest():
    """Panel «Vigilancia en vivo»: aviso de operaciones virtuales, en vivo frente al backtest, abiertas ahora y la
    próxima apertura por evaluar; textos escapados y sin «undefined» ni «NaN»."""
    html = run(VIGIL + """H.open(); reset(); H.recv(vig()); renderVigil(); return H.el("#vigil").innerHTML;""")
    assert "Operaciones VIRTUALES: sin dinero" in html and "nada &lt;b&gt;" in html
    assert '<span class="pill good">Al día</span>' in html
    assert "cuenta desde el 27/10 (lista congelada el 26/10)" in html and "3 aperturas evaluadas" in html
    assert "Próxima apertura por evaluar: Tokio y Seúl del 28/10 (abre a las 01:00; se evalúa hacia las 05:01" in html
    assert "&lt;script&gt;" in html and "<script>" not in html
    assert ("stop 3,0 % · objetivo 3,0 R (+9,00 %) · hasta la misma apertura del día siguiente · sin break-even ni stop "
            "dinámico") in html
    assert "backtest fuera de muestra 2025-08-16 a 2026-10-23" in html
    assert '<span class="pos">+0,51 R</span>' in html and '<span class="pos">+0,14 R</span>' in html
    assert ">50 %<" in html and ">52 %<" in html and ">278<" in html and '<span class="pos">+0,20 R</span>' in html
    # En vivo con los cortos en perpetuo y, al lado, con los costes del backtest (el comparable exacto).
    assert "cortos en perpetuo" in html and "costes del backtest" in html and "costes de contado" in html
    assert '<span class="pos">+0,50 R</span>' in html and "Costes de cada columna" in html
    assert '<span class="pill muted">hipótesis congelada</span>' in html and "la regla que opera el paper" not in html
    assert "intervalo aproximado del R medio en vivo (95 %): -1,10 a +2,12" in html
    assert "con 0 operaciones cerradas en vivo aún no se puede comparar con el backtest" in html
    assert 'XRP <span class="pos">largo</span> desde 27/10 15:00 (Nueva York): entrada 1,5203' in html
    assert 'ahora <span class="neg">-0,12 R</span>' in html and "corto (perpetuo)" in html
    assert "Nueva York 27/10: E2 XRP largo (abierta)" in html and "Europa 27/10: ninguna entra" in html
    for bad in ("undefined", "NaN", "null", "Invalid"):
        assert bad not in html, bad


def test_vigilancia_tolera_campos_ausentes_y_la_simulacion_avisa():
    r = run(VIGIL + """H.open(); reset();
      renderVigil(); const before = H.el("#vigil").innerHTML;
      H.recv({type: "vigilancia"}); renderVigil(); const a = H.el("#vigil").innerHTML;
      H.recv({type: "vigilancia", estado: "datos_atrasados", nota: "Las velas no llegan", desde: 1e300, datos_hasta: "x",
              proxima: {open_ms: "x"}, aperturas: "rota", estrategias: [null, {}, {vivo: null, backtest: null, abiertas: "x",
              ultimas: [null]}, {...E2, vivo: {ic95: [null, 1]}, abiertas: [{moneda: "BTC"}]}]});
      renderVigil(); const b = H.el("#vigil").innerHTML;
      S.tab = "market"; renderMarket();
      return {before, a, b};""")
    assert "aún no ha publicado nada" in r["before"]
    assert "Sin estrategias vigiladas" in r["a"] and "sin estado" in r["a"]
    assert "Velas atrasadas" in r["b"] and "Las velas no llegan" in r["b"] and "BTC – desde –" in r["b"]
    for html in (r["a"], r["b"]):
        for bad in ("undefined", "NaN", "null", "Invalid"):
            assert bad not in html, bad
    sim = run("""renderVigil(); return H.el("#vigil").innerHTML;""", live=False)
    assert "La demostración no simula la vigilancia" in sim


def test_vigilancia_variante_del_usuario_de_7_dias():
    """La variante de 7 días va rotulada aparte (la que opera el paper), con la cifra de su réplica si la hay o
    avisando de que no tiene; las operaciones sin datos tras un apagón largo se cuentan aparte."""
    html = run(VIGIL + """H.open(); reset();
      const V7 = {...E2, id: E2.id.replace("|sig", "|7d"), codigo: "E2-7d", clase: "variante", origen: "variante",
        variante_de: E2.id, en_paper: true, nombre: "Movimiento de los primeros minutos · sin tope de tiempo",
        reglas: {...E2.reglas, dur: "7d", dur_texto: "sin tope de tiempo (7 días de seguridad)"},
        vivo: {...E2.vivo, sin_datos: 1},
        backtest: {operaciones: 202, R_medio: 0.3459, ganadoras: 0.4109, R_medio_cortos_perp: 0.5626,
                   periodo: "2025-08-16 a 2026-10-23", fuente: "réplica aparte con el laboratorio (no congelada, sin validar)"}};
      const V1 = {...E1, id: E1.id.replace("|sig", "|7d"), codigo: "E1-7d", clase: "variante", origen: "variante",
        variante_de: E1.id, en_paper: false, backtest: null};
      H.recv(vig({estrategias: [E1, V1, E2, V7]})); renderVigil(); return H.el("#vigil").innerHTML;""")
    assert html.count("variante del usuario · 7 días") == 2 and html.count("la regla que opera el paper") == 1
    assert "la misma estrategia que E2 sin tope de tiempo (solo los 7 días de seguridad)" in html
    assert "la misma estrategia que E1 sin tope de tiempo" in html
    assert "backtest fuera de muestra 2025-08-16 a 2026-10-23 (réplica aparte con el laboratorio (no congelada, sin validar))" in html
    assert "los cortos en perpetuo dan +0,56 R de media" in html and '<span class="pos">+0,35 R</span>' in html
    assert "sin cifra del backtest: no está en la lista congelada" in html
    assert "1 sin datos para resolverlas (apagón largo): no cuentan" in html
    for bad in ("undefined", "NaN", "null", "Invalid"):
        assert bad not in html, bad


def test_resultados_por_noticia_y_lado():
    """Resultados: el paper agrupado por la etiqueta de la noticia (o la del nombre de estrategia del motor) y el lado."""
    html = run("""H.open(); reset();
      const t = (strategy, side, pnl, R, tags) => ({strategy, side, pnl, R, tags, coin: "BTC", window: "Apertura Europa",
        entry: 100, exit: 101, reason: "Objetivo", pnl_pct: 1, fees: 0.1, opened: now - 3600e3, closed: now});
      S.results = {start_equity: 10000, equity: 10030, by_strategy: [], by_window: [], by_coin: [], equity_curve: [], daily: [],
        trades: [t("impulso-noticia-a-favor", "long", 20, 2, {noticia: "a_favor"}),
                 t("impulso-noticia-a-favor", "long", -10, -1, {noticia: "a_favor"}),
                 t("impulso-noticia-en-contra", "short", 30, 3), t("impulso-sin-noticia", "short", -10, -1, {noticia: "neutral"}),
                 t("seguir-bolsa", "long", 0, 0)]};
      renderResults(); return H.el("#resByNews").innerHTML;""")
    rows = html.split("<tr>")[1:]
    assert len(rows) == 4
    assert "A favor" in rows[0] and "Largo (compra)" in rows[0] and ">2<" in rows[0] and ">50 %<" in rows[0]
    assert ">0,50<" in rows[0] and "+10,00" in rows[0]
    assert "Neutral" in rows[1] and "Corto (venta)" in rows[1] and ">0 %<" in rows[1] and "-1,00" in rows[1]
    assert "En contra" in rows[2] and "Corto (venta)" in rows[2] and ">100 %<" in rows[2] and "+30,00" in rows[2]
    assert "Sin etiqueta (otras estrategias)" in rows[3] and "Largo (compra)" in rows[3]
    empty = run("""H.open(); reset(); S.results = {start_equity: 10000, equity: 10000, trades: []}; renderResults();
      return H.el("#resByNews").innerHTML;""")
    assert "Sin datos todavía" in empty


def test_resultados_muestran_instrumento_y_funding():
    """Todas las operaciones: el lado lleva el instrumento (perpetuo o contado) y los costes, el funding cobrado o pagado."""
    html = run("""H.open(); reset();
      const t = (side, instrument, funding, commissions) => ({strategy: "impulso-corto-sin-noticia", side, instrument, funding, commissions,
        pnl: 10, R: 1, coin: "ETH", window: "Apertura Europa", entry: 100, exit: 99, reason: "Objetivo", pnl_pct: 0.1, fees: 1.95,
        opened: now - 3600e3, closed: now, story: "s1"});
      S.results = {start_equity: 10000, equity: 10010, by_strategy: [], by_window: [], by_coin: [], equity_curve: [], daily: [],
        trades: [t("short", "perp", 0.15, 2.1), t("long", "spot", 0, 2.0), t("long", null, null, null)]};
      renderResults(); return H.el("#resRows").innerHTML;""")
    assert 'VENTA</span> <span class="note">perpetuo</span>' in html and 'COMPRA</span> <span class="note">contado</span>' in html
    assert "funding +0,15" in html and "funding cobrado 0,15 USDC (paper)" in html and "Comisiones 2,10" in html
    assert html.count("funding") == 2   # solo la operación con funding
    for bad in ("undefined", "NaN", "null"):
        assert bad not in html, bad


# Motor de aperturas en modo impulso (bot/apertura.py, build_status con strategy «impulso»): fases nuevas, paso de la
# entrada, lado e instrumento, niveles y plan con % bruto, costes, funding y neto. Sacado de un estado real del motor.
IMPULSO = r"""
const IMP = {estrategia: "impulso", measure_min: 15, min_move_pct: 0.2, entry: "t:30", pb_frac: 0.5, entry_end_min: 120, brk_tf: 1,
  rvol_min: 1.5, vol_filter: "todos", btc_filter: "a_favor", news_mode: "no_contra", windows: ["tokio-seul", "china", "europa", "saopaulo", "nuevayork"],
  coins: ["BTC", "ETH", "SOL", "XRP"], sl_pct: 3, target_r: 3, target_pct: null, max_hold_h: 168, min_net_pct: 3, avg_hold_h: 65,
  long_instrument: "spot", short_instrument: "perp", min_strength: 0.2, risk_pct: null, max_day_entries: 4, max_day_risk_pct: 2,
  session_max_min: 120, lab: "ap_mom:15:0,2|rvol:1,5|todos|a_favor|todas|todas|todos|t:30|3|3|sig"};
const nada = (state, why) => ({signal: 0, strength: 0, why, last_why: why, drivers: [], news: {dir: 0, strength: 0, why: "", label: null},
  range: null, rvol: null, buy_ratio: null, entries: 0, results: [], state, step: null, instrument: null, gate: null, moving: null,
  plan: null, tags: null, levels: null, context: null});
const tags = (lado, instrumento, mom) => ({lado, instrumento, noticia: "neutral", noticia_fuerza: 0, apertura_anterior: "sin_dato",
  apertura_anterior_pct: null, btc: lado === "largo" ? "a_favor" : "en_contra", volatilidad_pct: null, impulso_pct: mom, medida_min: 15, rvol: 3});
const BTC = {signal: 1, strength: 0, why: "En posición (paper): compra al contado a 100,31", last_why: "En posición (paper): compra al contado a 100,31",
  drivers: [], news: {dir: 0, strength: 0, why: "sin noticias", label: "neutral"}, range: {hi: 100.301, lo: 99.999}, rvol: 3, buy_ratio: null,
  entries: 1, results: [], state: "en_posicion", step: null, instrument: "spot", gate: null, moving: null,
  plan: {stopPct: 3, tp2R: 3, rr: 3, ok: true, why: "Horquilla pura desde 100,31 (contado)", instrument: "spot", gross_pct: 9, cost_pct: 0.24,
         funding_pct: 0, net_pct: 8.76, entry: 100.31, stop: 97.3007, target: 109.3379},
  tags: tags("largo", "spot", 0.3), levels: {apertura: 100, impulso_pct: 0.3, maximo: 100.301, minimo: 99.999, extremo: 100.301, retroceso: null,
  ruptura: null, hora_entrada: now - 60e3, rvol: 3, entrada: 100.31, stop: 97.3007, objetivo: 109.3379}, context: {atr_pct: null, btc_dir: 1}};
const ETH = {...BTC, signal: -1, why: "En posición (paper): venta en perpetuo a 99,5000", last_why: "En posición (paper): venta en perpetuo a 99,5000",
  instrument: "perp", tags: tags("corto", "perp", -0.5),
  plan: {stopPct: 3, tp2R: 3, rr: 3, ok: true, why: "Horquilla pura desde 99,5000 (perpetuo) <b>", instrument: "perp", gross_pct: 9, cost_pct: 0.24,
         funding_pct: 0.1625, net_pct: 8.597, entry: 99.5, stop: 102.485, target: 90.545},
  levels: {apertura: 100, impulso_pct: -0.5, maximo: 100.001, minimo: 99.499, extremo: 99.499, retroceso: null, ruptura: null,
           hora_entrada: now - 60e3, rvol: 3, entrada: 99.5, stop: 102.485, objetivo: 90.545}};
const SOL = {...nada("esperando_entrada", "Impulso de +0,30 % en 15 min (compra al contado): entro a +30 min (09:30 Madrid)"), signal: 1,
  step: "hora", instrument: "spot", range: {hi: 150.4, lo: 149.9}, tags: tags("largo", "spot", 0.3),
  levels: {apertura: 150, impulso_pct: 0.3, maximo: 150.4, minimo: 149.9, extremo: 150.4, retroceso: null, ruptura: null,
           hora_entrada: now + 14 * 60e3, rvol: null, entrada: null, stop: null, objetivo: null}};
const XRP = {...nada("midiendo_impulso", "Midiendo el impulso: +0,16 % en 8 de 15 min"), range: {hi: 1.52, lo: 1.5}};
const imp = (o = {}) => ({type: "openings", ts: now, strategy: "impulso", params: IMP, params_source: "preset_impulso", notice: null,
  states: ["esperando_apertura", "midiendo_impulso", "esperando_entrada", "en_posicion", "terminada"],
  day: {date: "2026-10-27", entries: 2, risk_pct: 1, max_entries: 4, max_risk_pct: 2},
  windows: [{id: "europa-x", name: "Europa", markets: [], open_utc: now - 30 * 60e3, state: "en_posicion", why: "Se sigue el impulso…",
             coins: {BTC, ETH, SOL, XRP}},
            {id: "nuevayork-x", name: "Nueva York", markets: [], open_utc: now + 5 * 3600e3, state: "esperando_apertura", why: "Se sigue el impulso…",
             coins: {BTC: nada("esperando_apertura", "A las 15:30 Madrid se mide el impulso de los 15 primeros min")}},
            {id: "china-x", name: "Shanghái y Hong Kong", markets: [], open_utc: now - 9 * 3600e3, state: "terminada",
             why: "La sesión de esta apertura pasó con el bot parado: no se siguió", coins: {BTC: nada("terminada", "La sesión pasó con el bot parado")}}],
  ...o});
"""


def test_motor_aperturas_modo_impulso():
    """Panel del motor en modo impulso: parámetros en claro, fases nuevas, paso de la entrada, lado e instrumento,
    niveles y plan con % bruto, costes, funding y neto; la pausa de la evolución se avisa; escapado y sin null."""
    r = run(IMPULSO + """H.open(); reset();
      H.recv(imp({estrategia_cargada: {firma: "ab12cd34", ts: now - 3600e3, pausada: true, fuente: "manual", registro: "v9",
                                       motivo: "Pausado por la evolución: R contraído −0,42 <b>"}}));
      renderOpenings();
      const sig = coinSignal("SOL", newsBalance(Date.now()), Date.now());
      return {html: H.el("#openings").innerHTML, sub: H.el("#openSub").textContent, sig};""")
    html = r["html"]
    assert r["sub"].startswith("Modo impulso (paper)")
    for t in ("Modo impulso", "Preset impulso", "del preset IMPULSO", "impulso de los 15 primeros min (≥ ±0,20 %)",
              "entrada a +30 min", "volumen relativo ≥ 1,5", "solo a favor de la tendencia de BTC", "todas las aperturas",
              "stop 3,00 % y objetivo 3,0 R (+9,00 %)", "sin tope de tiempo (7 días de seguridad)", "≥ 3,0 % neto de costes",
              "largos al contado, cortos en perpetuo", "reproduce la prometedora del laboratorio", "sin break-even ni stop dinámico",
              "2 de 4 entradas"):
        assert t in html, t
    assert "Pausado por la evolución (solo paper)" in html and "&lt;b&gt;" in html and "−0,42 <b>" not in html
    assert "Versión cargada" in html and "ab12cd34" in html and "aplicada desde la pestaña Evolución" in html
    for t in ("En posición", "Esperando la entrada", "Midiendo el impulso", "Esperando la apertura", "Terminada",
              "entra a la hora fijada", "La sesión de esta apertura pasó con el bot parado"):
        assert t in html, t
    assert '<span class="pos">▲ Largo</span><br><span class="note">contado</span>' in html
    assert '<span class="neg">▼ Corto</span><br><span class="note">perpetuo</span>' in html
    assert ">+0,30 %</b>" in html and ">-0,50 %</b>" in html and "en 15 min" in html and "Sin noticia clara" in html
    assert "BTC a favor" in html and "BTC en contra" in html and "apertura anterior sin dato" in html
    assert "entrada 100,31 · stop 97,30 · objetivo 109,34" in html and "hora de entrada" in html and "×3,00" in html
    assert "En posición (paper): neto 8,76 % (mínimo 3,0 %)" in html and "bruto +9,00 % · costes −0,24 % · sin funding (contado)" in html
    assert "funding −0,16 %" in html and "neto 8,60 %" in html and "Funding esperado en 65 h" in html
    assert "Sin plan todavía" in html and "(perpetuo) &lt;b&gt;" in html
    for bad in ("undefined", "NaN", "null", "Invalid"):
        assert bad not in html, bad
    assert r["sig"]["mom"] == "wait" and r["sig"]["momTxt"].startswith("Impulso de +0,30 %")


def test_motor_impulso_tolera_campos_ausentes():
    r = run(IMPULSO + """H.open(); reset();
      H.recv({type: "openings", strategy: "impulso", params: {entry: "pb", rvol_min: null, windows: ["china", "raro"], coins: ["BTC"]},
              params_source: "preset_impulso+entorno", estrategia_cargada: {firma: null, ts: "x", pausada: false},
              windows: [{name: "Rota", open_utc: "x", state: "rara", coins: {BTC: {state: "esperando_entrada", step: "raro", levels: {hora_entrada: 1e300},
                plan: {ok: false, why: "Regla del usuario: quedan 2,76 % netos < 3 %"}, tags: {lado: "corto"}}, ETH: null}}]});
      renderOpenings(); return H.el("#openings").innerHTML;""")
    assert "Preset + entorno" in r and "entrada en la reanudación tras retroceder parte del impulso" in r
    assert "sin filtro de volumen" in r and "Shanghái y Hong Kong, raro" in r and "BTC" in r
    assert "Plan rechazado: neto sin calcular" in r and "quedan 2,76 % netos &lt; 3 %" in r and "▼ Corto" in r
    assert "Versión cargada" not in r and "Pausado" not in r
    for bad in ("undefined", "NaN", "null", "Invalid"):
        assert bad not in r, bad


# Mensaje «evolucion» (bot/evolucion.py, Evolucion._mensaje): campeón, aspirantes, propuestas, historial e informe.
EVO = r"""
const D = 86400e3;
const curva = (rs, t0) => { let a = 0; return rs.map((r, i) => ({semana: "2026-W" + (40 + i), lunes: t0 + i * 7 * D, operaciones: 3, R: r, R_acum: (a += r)})); };
const EF = {measure_min: 15, min_move_pct: 0.2, entry: "t:30", rvol_min: 1.5, vol_filter: "todos", btc_filter: "a_favor", news_mode: "no_contra",
  windows: ["tokio-seul", "china", "europa", "saopaulo", "nuevayork"], coins: ["BTC", "ETH", "SOL", "XRP"], sl_pct: 3, target_r: 3, max_hold_h: 168, min_net_pct: 3};
const evo = (o = {}) => ({type: "evolucion", ts: now, estado: "ok", nota: null, modo: "proponer",
  aviso: "Solo paper: nada de esto opera con dinero real <b>", reglas: {texto: "Stop ≥ 1,5 %, objetivo ≥ 2R y ≥ 3 % neto"},
  criterios: {texto: "Un aspirante sustituye al campeón con ≥ 40 operaciones nuevas"},
  campeon: {nombre: "Impulso de apertura", codigo: "E2-7d", lab_id: null, fuente: "preset", pausada: false, desde: now - 30 * D, firma: "ab12cd34ef",
    confirmado: false, pendiente_reinicio: true, motor: {texto: "Pendiente: el motor aún no ha confirmado esta versión; carga estrategia_activa.json solo en ≤ 1 min"},
    params: {}, efectivos: EF, reglas: {ok: true, fallos: []},
    vivo: {operaciones: 22, R_medio: 0.18, R_total: 3.96, ganadoras: 0.41, R_contraido: 0.09, n_efectivo: 21.3, ic95: [-0.3, 0.66], dias: 15, muestra: "poca muestra: orientativo"},
    en_curso: 3, sin_resolver: 0, curva: curva([1, -0.5, 2], now - 21 * D), gemelo: {id: "x|7d", codigo: "E2-7d", filtro: null, texto: null},
    virtual: {operaciones: 25, R_medio: 0.2, R_contraido: 0.1, ganadoras: 0.4, ic95: null, muestra: "poca muestra: orientativo"}, virtual_en_curso: 2,
    curva_virtual: curva([0.8, -0.2, 1.5], now - 21 * D), brecha_paper_virtual: {texto: "Brecha paper–virtual: −0,02 R"}, degradado: false,
    degradacion: {evaluada: false, operaciones: 22, R_medio: 0.18, R_contraido: 0.09, ic95: null, p_menor: null, evidencia_clara: false, datos_desde: null}},
  aspirantes: [
    {id: "btc:ma20|x|7d", codigo: "E1-7d", nombre: "Tendencia diaria de BTC <i>", clase: "variante", origen: "variante", desde: now - 20 * D,
     backtest: {operaciones: 34, R_medio: 0.2048, ganadoras: 0.5588}, dur: "7d", variante_de: "btc:ma20|x|sig", compatible: true, por_que_no: null,
     params: {}, reglas_ok: true, vivo: {operaciones: 12, R_medio: 0.35, R_contraido: 0.13, ganadoras: 0.42, ic95: null, muestra: "poca muestra: orientativo"},
     en_curso: 1, sin_resolver: 0, curva: curva([0.5, 1, -0.3], now - 21 * D),
     frente: {referencia: "virtual", texto: "operaciones virtuales del gemelo del campeón", desde: now - 20 * D, operaciones: 12, R_medio: 0.35,
              R_contraido: 0.13, ref_operaciones: 25, ref_R_medio: 0.2, paper_operaciones: 22, paper_R_medio: 0.18, dif_R: 0.15, p: 0.31,
              ic95: [-0.4, 0.7], pruebas: 1, p_max_corregido: 0.05, bloqueo_desde: null, cumple: false,
              faltan: ["lleva 12 operaciones nuevas completas comparables (hacen falta 40)"], confirmacion: null, clave: "promo|x|y"}},
    {id: "btc:ma20|x|sig", codigo: "E1", nombre: "E1 hasta la apertura siguiente", clase: "prometedora", origen: "congelada", compatible: false,
     por_que_no: "su salida no es la del motor (E1-7d)", reglas_ok: false,
     vivo: {operaciones: 0, R_medio: null, R_contraido: 0, ganadoras: null, ic95: null, muestra: "sin datos"}, curva: [],
     frente: {operaciones: 0, ref_operaciones: 25, dif_R: null, p: null, pruebas: null, p_max_corregido: null, cumple: false,
              faltan: ["el motor no sabe operarla"], confirmacion: {estado: "esperando", texto: "Falta confirmarlo con 10 operaciones"}}}],
  pruebas: 1,
  propuestas: [{id: "p1", ts: now - D, tipo: "estrategia_promovida", sujeto: "E1-7d", titulo: "Sustituir el campeón por E1-7d",
    texto: "E1-7d lleva 41 operaciones <script>", cambio: {},
    evidencia: {operaciones: 41, R_medio: 0.35, ref_R_medio: 0.2, dif_R: 0.15, p: 0.012, pruebas: 1, p_max_corregido: 0.05, ic95: [0.02, 0.3]},
    actualizada: now - D, bloqueada: null, automatica: false}],
  recientes: [{id: "p0", ts: now - 9 * D, tipo: "parametro_cambiado", titulo: "Dejar de operar XRP", estado: "descartada", resuelta_ts: now - 8 * D,
               por: "usuario (web)", nota: "No me convence"}],
  bloqueos: [{clave: "ajuste|moneda|XRP|f", ts: now - 8 * D, motivo: "descartada", por: "usuario (web)", propuesta: "p0"}],
  cambios: [
    {id: "v3", ts: now - 2 * D, tipo: "fuente_silenciada", sujeto: "ruido.example", antes: {estado: "activa", peso: 1}, despues: {estado: "silenciada", peso: 0},
     motivo: "Ruido 93 % <script>alert(1)</script>", origen: "fuentes_vivas", revierte: null, etiqueta: "Medio silenciado", revertida_por: null, reversible: true,
     evidencia: {casos: 212, independientes: 41, ruido: 0.93, regla: "ruido", fuente_id: "medio:ruido.example", deshacer: {accion: "restaurar", id: "medio:ruido.example"}}},
    {id: "v2", ts: now - 5 * D, tipo: "peso_cambiado", sujeto: "agencia.example", antes: {peso: 1}, despues: {peso: 1.18}, motivo: "Acierta más que la base",
     evidencia: {acierto: 0.61, casos: 143, cambio_id: 7}, origen: "aprendizaje", revierte: null, etiqueta: "Peso cambiado", revertida_por: "v4", reversible: false},
    {id: "v6", ts: now - 3 * D, tipo: "veredicto_medio", sujeto: "agencia.example", antes: {veredicto: "neutro"}, despues: {veredicto: "acierta", peso: 1.18},
     motivo: "Acierta (veredicto: neutro → acierta)", evidencia: {cambio_id: 8}, origen: "aprendizaje", revierte: null, etiqueta: "Veredicto de un medio",
     revertida_por: null, reversible: false},
    {id: "v5", ts: now - 4 * D, tipo: "propuesta_descartada", sujeto: "ventana china", antes: {estado: "pendiente"}, despues: {estado: "descartada"},
     motivo: "Propuesta descartada por ti", evidencia: {propuesta: "p0"}, origen: "web", revierte: null, revertida_por: null, reversible: false},
    {id: "v1", ts: now - 7 * D, tipo: "informe_semanal", sujeto: "2026-W43", antes: null,
     despues: {titulo: "Informe de la semana del 19 al 25 de octubre", resumen: "Campeón: Impulso."}, motivo: "Informe semanal", evidencia: {},
     origen: "evolucion", revierte: null, etiqueta: "Informe semanal", revertida_por: null, reversible: false},
    {id: "v0", ts: now - 60 * D, tipo: "estrategia_promovida", sujeto: "E9", antes: {nombre: "A", codigo: "E8"}, despues: {nombre: "B", codigo: "E9"},
     motivo: "Promoción antigua", evidencia: {}, origen: "evolucion", revierte: null, etiqueta: "Estrategia promovida", revertida_por: null, reversible: true},
    {id: "v7", ts: now - 6 * D, tipo: "estrategia_degradada", sujeto: "Sin marca", antes: {pausada: false}, despues: {pausada: true},
     motivo: "Entrada de un bot antiguo sin «reversible»", evidencia: {}, origen: "evolucion", revierte: null, revertida_por: null}],
  informe: {id: "v1", ts: now - 7 * D, semana: "2026-W43", titulo: "Informe de la semana del 19 al 25 de octubre", resumen: "Campeón: Impulso."},
  ultima_evaluacion: now - 7 * D, proxima_evaluacion: now + 3 * D, ultimo_cambio: null, automatico_desde: null, ultima_orden: null, ...o});
const EVO_IDS = ["#evoCtl", "#evoHead", "#evoProps", "#evoChamp", "#evoAsp", "#evoFilters", "#evoTimeline", "#evoMediaHead", "#evoMediaF",
  "#evoMedia", "#evoMedio", "#evoBest", "#evoWorst", "#evoCand", "#evoReport"];
const evoHtml = () => Object.fromEntries(EVO_IDS.map(id => [id, H.el(id).innerHTML]));
"""


def _sin_null(*htmls):
    for html in htmls:
        for bad in ("undefined", "NaN", "null", "Invalid", "[object"):
            assert bad not in html, bad


def test_evolucion_pinta_campeon_aspirantes_propuestas_e_historial():
    """Pestaña Evolución con el mensaje «evolucion»: modo, reglas, propuesta con sus cifras y botones, campeón con su
    gemelo y su curva semanal, aspirantes frente al gemelo, línea de tiempo con motivo, cifras y Revertir, e informe."""
    r = run(EVO + """H.open(); reset(); H.recv(evo());
      H.fire("click", {"data-tab": "evo"}); renderAll(true);
      const a = evoHtml(), badge = [H.el("#evoBadge").hidden, String(H.el("#evoBadge").textContent)];
      H.fire("click", {"data-evo-dias": "0"}); renderAll(true); const todo = H.el("#evoTimeline").innerHTML;
      const tg = H.target({"data-evo-f": "tipo"}); tg.value = "fuente_silenciada"; H.fire("change", {}, {target: tg}); renderAll(true);
      return {a, badge, todo, tipo: H.el("#evoTimeline").innerHTML, hash: HASH_OF.evo};""")
    a = r["a"]
    assert r["badge"] == [False, "1"] and r["hash"] == "evolucion"
    assert 'data-evo-modo="proponer" aria-pressed="true"' in a["#evoCtl"] and 'data-evo-eval="1"' in a["#evoCtl"]
    head = a["#evoHead"]
    assert "Modo <b>proponer</b>" in head and "1 propuesta pendiente" in head and "dinero real &lt;b&gt;" in head
    assert "Tus reglas, intocables:" in head and "Cuándo se cambia algo" in head
    props = a["#evoProps"]
    assert "Sustituir el campeón por E1-7d" in props and "41 operaciones &lt;script&gt;" in props and "<script>" not in props
    assert 'data-evo-aplicar="p1"' in props and 'data-evo-descartar="p1"' in props
    assert "operaciones 41" in props and "p 0,0120" in props and "IC 95 % +0,02 a +0,30" in props
    assert "Resueltas hace poco" in props and "Descartada" in props and "No me convence" in props and "Bloqueadas por tu decisión" in props
    ch = a["#evoChamp"]
    assert "Impulso de apertura" in ch and "Pendiente de carga" in ch and "preset del motor" in ch and "versión ab12cd34ef" in ch
    assert "reinicio" not in ch and "no hace falta reiniciar" in ch
    assert "impulso de los 15 primeros min (≥ ±0,20 %)" in ch and "sin tope de tiempo (7 días de seguridad)" in ch
    assert "22 operaciones completas · R medio +0,18 R · contraído hacia 0: +0,09 R · 41 % ganadoras · IC 95 % -0,30 a +0,66" in ch
    assert "3 en curso aún no cuentan" in ch and "Gemelo virtual E2-7d:</b> 25 operaciones completas" in ch
    assert "Brecha paper–virtual" in ch and "no se evalúa hasta que el motor confirme esta versión" in ch
    assert "<svg" in ch and "stroke-dasharray" in ch and "Campeón (paper)" in ch
    asp = a["#evoAsp"]
    assert "E1-7d" in asp and "Tendencia diaria de BTC &lt;i&gt;" in asp
    assert "backtest del laboratorio: 34 op. fuera de muestra, R medio +0,20 R, 56 % ganadoras" in asp
    assert "El motor puede operarla" in asp and "12 frente a 25 op. · diferencia +0,15 R · p 0,3100" in asp
    assert "hace falta p &lt; 0,0500 (Holm, 1 aspirante comparable)" in asp and "campeón en paper: +0,18 R en 22 op." in asp
    assert "hacen falta 40" in asp and "No comparable" in asp and "su salida no es la del motor" in asp
    assert "Falta confirmarlo" in asp and "<svg" in asp
    tl = a["#evoTimeline"]
    assert "Medio silenciado" in tl and "ruido.example" in tl and "&lt;script&gt;alert(1)" in tl and "<script>" not in tl
    assert "activa · peso 1,00 → silenciada · peso 0,00" in tl and "ruido 93 %" in tl and "casos 212" in tl and "regla «ruido»" in tl
    assert "origen: ciclo de vida de las fuentes" in tl and 'data-evo-revertir="v3"' in tl
    assert 'data-evo-revertir="v2"' not in tl and "revertido" in tl and "peso 1,00 → peso 1,18" in tl
    assert 'data-evo-revertir="v1"' not in tl and "Campeón: Impulso." in tl and "E9" not in tl   # 60 días: fuera de los 30
    # Solo se ofrece Revertir donde el bot dice que se puede: ni un veredicto, ni una propuesta descartada, ni sin marca
    assert "Veredicto de un medio" in tl and "neutro → acierta · peso 1,18" in tl and 'data-evo-revertir="v6"' not in tl
    assert "Propuesta descartada" in tl and 'data-evo-revertir="v5"' not in tl and 'data-evo-revertir="v7"' not in tl
    assert "Promoción antigua" in r["todo"] and "E8 · A → E9 · B" in r["todo"] and 'data-evo-revertir="v0"' in r["todo"]
    assert "ruido.example" in r["tipo"] and "agencia.example" not in r["tipo"]
    assert 'data-evo-f="tipo"' in a["#evoFilters"] and "Todos los orígenes" in a["#evoFilters"] and "aprendizaje de medios" in a["#evoFilters"]
    assert "Informe de la semana del 19 al 25 de octubre" in a["#evoReport"] and "Campeón: Impulso." in a["#evoReport"]
    _sin_null(*a.values(), r["todo"], r["tipo"])


def test_evolucion_ordenes_por_websocket():
    """Aplicar, descartar, revertir, modo y evaluar van por el WebSocket con los nombres de bot/evolucion.py; con
    confirm cancelado no se envía nada; el resultado (ultima_orden) sale en un aviso y sin conexión se avisa."""
    r = run(EVO + """H.open(); reset(); H.recv(evo());
      H.fire("click", {"data-evo-aplicar": "p1"}); H.fire("click", {"data-evo-descartar": "p1"}); H.fire("click", {"data-evo-revertir": "v3"});
      H.fire("click", {"data-evo-modo": "automatico"}); H.fire("click", {"data-evo-modo": "proponer"}); H.fire("click", {"data-evo-eval": "1"});
      const sent = H.sent().slice(), t0 = H.el("#toast").textContent;
      H.confirmAnswer = false; H.fire("click", {"data-evo-aplicar": "p1"}); H.fire("click", {"data-evo-revertir": "v3"});
      H.fire("click", {"data-evo-modo": "automatico"}); const n2 = H.sent().length; H.confirmAnswer = true;
      H.recv(evo({ultima_orden: {accion: "aplicar", id: "p1", ok: false, why: "Ya no está respaldada <b>", ts: now + 5}}));
      const t1 = H.el("#toast").textContent;
      H.timeouts.filter(x => x.ms === 10000).forEach(x => x.f()); const t2 = H.el("#toast").textContent;
      H.timeouts.length = 0; H.fire("click", {"data-evo-eval": "1"}); H.timeouts.filter(x => x.ms === 10000).forEach(x => x.f());
      const t3 = H.el("#toast").textContent;
      H.recv(evo({ultima_orden: {accion: "evaluar", id: null, ok: true, why: null, ts: now + 9}})); const t4 = H.el("#toast").textContent;
      H.ws().close(); H.fire("click", {"data-evo-aplicar": "p1"});
      return {sent, t0, n2, t1, t2, t3, t4, t5: H.el("#toast").textContent, total: H.sockets[0].sent.length};""")
    assert r["sent"] == [{"type": "evolucion", "accion": "aplicar", "id": "p1"},
                         {"type": "evolucion", "accion": "descartar", "id": "p1", "motivo": ""},
                         {"type": "evolucion", "accion": "revertir", "id": "v3"},
                         {"type": "evolucion", "accion": "modo", "modo": "automatico"},
                         {"type": "evolucion", "accion": "evaluar"}]
    assert "Evaluación pedida" in r["t0"] and r["n2"] == 5
    assert r["t1"] == "Evolución: no se pudo «aplicar»: Ya no está respaldada <b>."
    assert r["t2"] == r["t1"]   # respondió: no se avisa de que no respondiera
    assert "La evolución no ha respondido" in r["t3"]
    assert r["t4"] == "Evolución: «evaluar ahora» hecho (solo paper)."
    assert "Sin conexión con el bot" in r["t5"] and r["total"] == 6


# Mensajes «aprendizaje» (bot/aprendizaje.py) y «fuentes_vivas» (bot/fuentes_vivas.py) para el ranking de medios.
MEDIOS = r"""
const M = (medio, o = {}) => ({medio, nombre: medio, nivel: "T2", region: "global", idioma: "es", casos: 60, independientes: 22, dias: 11,
  acierto: 0.5, ic90: [0.42, 0.58], bruto: 0.62, base: 0.6, acierto_sin_ajuste: 0.52, ret_1h: 0.05, ret_4h: 0.08, accionable: 0.5,
  ventanas: 0.5, ventanas_casos: 6, primera: 0.2, adelanto_min: 2.5, ruido: 0.2, fuera_de_tema: 0.3, archivo: 0.1, piezas: 90, ops: 0, R: 0,
  peso: 1, peso_manual: null, veredicto: "neutro", compatible: "si", directo: true, clave_tipo: "dominio", alias: [], candidato: false, nota: "Neutro.", ...o});
const apr = (o = {}) => ({type: "aprendizaje", ts: now, estado: "ok", nota: "Nota <b>", aviso: "Aviso de honestidad", params: {min_casos: 20, min_dias: 10},
  horizontes: [15, 60, 240, 1440], totales: {medios: 4, medios_con_datos: 2, historias: 50},
  global: {clave: "todas", casos: 50, independientes: 21, dias: 11, acierto: 0.52, ic90: [0.45, 0.59], bruto: 0.7, base: 0.66},
  medios_mejores: [M("agencia.example", {nombre: "Agencia <b>", acierto: 0.61, ic90: [0.54, 0.68], independientes: 27, peso: 1.18, veredicto: "acierta",
                                         nota: "Acierta más que la base <b>"})],
  medios_peores: [M("canal.example", {acierto: 0.41, ic90: [0.34, 0.48], peso: 0.82, veredicto: "falla"})],
  medios_ruido: [M("ruido.example", {ruido: 0.93, veredicto: "ruido"})],
  medios_nuevos: [M("Boletín", {clave_tipo: "nombre", directo: false, veredicto: "pocos_datos", independientes: 5, dias: 3})],
  medios_candidatos: [M("semanal.example", {directo: false, candidato: true, nota: "Solo por agregadores"})], ...o});
const fvMsg = (o = {}) => ({type: "fuentes_vivas", ts: now, auto: false, descubrir: true, feeds: {activa: 10, prueba: 1, silenciada: 2, retirada: 0},
  medios: {activa: 5, prueba: 0, silenciada: 1, retirada: 0}, candidatos: {}, ruido_medio: 0.3, sugerencias: 2,
  revision: {ultima: now - 3600e3, s: 1.2, medidas: 30, cambios: 1, anadidas: 0, nota: null}, cambios: [], aviso: "Solo cambia qué noticias cuentan", ...o});
const LISTA = [
  {id: "medio:ruido.example", tipo: "medio", nombre: "ruido.example", estado: "silenciada", peso: 0, manual: false, acciones: ["restaurar", "retirar"], sugerencia: null},
  {id: "medio:canal.example", tipo: "medio", nombre: "canal.example", estado: "activa", peso: 1, manual: true, manual_hasta: now + 20 * 86400e3,
   acciones: ["silenciar", "retirar"], sugerencia: {estado: "silenciada", motivo: "Acierta poco"}},
  {id: "medio:viejo.example", tipo: "medio", nombre: "viejo.example", estado: "retirada", peso: 0, manual: false, acciones: ["restaurar"]},
  {id: "jp-gn1", tipo: "feed", nombre: "Google News JP", estado: "activa", peso: 1, acciones: ["silenciar", "retirar"]}];
"""


def test_evolucion_ranking_de_medios_y_botones_de_fuentes():
    """Ranking de medios: acierto ajustado con su intervalo, muestra, aperturas, adelanto, ruido, peso, veredicto y el
    estado de fuentes_vivas; los botones envían {type:"fuente"} y avisan de la confirmación, del rechazo y sin conexión."""
    r = run(MEDIOS + """H.open(); reset(); H.recv(apr(), fvMsg()); S.fvList = LISTA; S.tab = "evo"; evoBump(); renderAll(true);
      const head = H.el("#evoMediaHead").innerHTML, html = H.el("#evoMedia").innerHTML, chips = H.el("#evoMediaF").innerHTML, cand = H.el("#evoCand").innerHTML;
      H.fire("click", {"data-fv-acc": "restaurar", "data-fv-id": "medio:ruido.example"}); const t1 = H.el("#toast").textContent;
      H.recv(fvMsg({cambios: [{ts: Date.now(), id: "medio:ruido.example", nombre: "ruido.example", accion: "restaurar", texto: "Restaurada",
                               antes: "silenciada", despues: "activa", motivo: "Decidido a mano desde la web"}]}));
      const t2 = H.el("#toast").textContent;
      H.timeouts.filter(x => x.ms === 10000).forEach(x => x.f()); const t3 = H.el("#toast").textContent; H.timeouts.length = 0;
      H.fire("click", {"data-fv-acc": "silenciar", "data-fv-id": "medio:canal.example"});
      H.timeouts.filter(x => x.ms === 10000).forEach(x => x.f()); const t4 = H.el("#toast").textContent;
      H.confirmAnswer = false; H.fire("click", {"data-fv-acc": "retirar", "data-fv-id": "medio:canal.example"}); const n = H.sent().length;
      H.confirmAnswer = true; H.fire("click", {"data-fv-acc": "borrar", "data-fv-id": "medio:canal.example"}); const n2 = H.sent().length;
      H.fire("click", {"data-evo-mf": "ruido"}); renderAll(true); const ruido = H.el("#evoMedia").innerHTML;
      H.fire("click", {"data-evo-mf": "apagados"}); renderAll(true); const apagados = H.el("#evoMedia").innerHTML;
      H.ws().close(); H.fire("click", {"data-fv-acc": "silenciar", "data-fv-id": "medio:agencia.example"});
      return {head, html, chips, cand, t1, t2, t3, t4, n, n2, ruido, apagados, t5: H.el("#toast").textContent, sent: H.sockets[0].sent};""")
    head, html = r["head"], r["html"]
    assert "Aprendizaje: Al día" in head and "Nota &lt;b&gt;" in head
    assert "acierto bruto 70 % frente a 66 % de la base de mercado → <b>ajustado 52 %</b> (IC 90 % 45 % a 59 %)" in head
    assert "21 casos independientes y 11 días" in head and "4 medios vistos · 2 con muestra suficiente" in head
    assert "solo sugiere: los cambios los haces tú" in head and "medios 5 activa, 0 en prueba, 1 silenciada, 0 retirada" in head
    assert "2 sugerencias" in head and "Solo cambia qué noticias cuentan" in head
    assert "Agencia &lt;b&gt;" in html and "Acierta más que la base &lt;b&gt;" in html and "<b>" not in html
    assert html.index("agencia.example") < html.index("canal.example")   # por peso
    assert '<span class="estchip activa">Activa</span>' in html and '<span class="estchip silenciada">Silenciada</span>' in html
    assert '<span class="estchip retirada">Retirada</span>' in html and "viejo.example" in html   # solo en fuentes_vivas
    assert 'data-fv-acc="restaurar" data-fv-id="medio:ruido.example"' in html and 'data-fv-acc="silenciar" data-fv-id="medio:agencia.example"' in html
    assert "jp-gn1" not in html and "a mano hasta el" in html and "sugerido: silenciada" in html
    assert "×1,18" in html and "61 %" in html and "bruto 62 % · base 60 %" in html and "27/20 bloques" in html and "11/10 días · 60 noticias" in html
    assert "+2,5 min" in html and "primera 20 %" in html and "93 %" in html and "fuera de tema 30 %" in html
    assert "Acierta" in html and "Falla" in html and "Ruido" in html and "Pocos datos" in html and "sin dominio" in html
    assert 'data-fv-id="medio:Boletín"' not in html and 'class="icbar"' in html
    assert "Todos · 6" in r["chips"] and "Ruido · 1" in r["chips"] and "Silenciados o retirados · 2" in r["chips"]
    assert "semanal.example" in r["cand"] and "Solo llegan por agregadores</b> y no son ruido ni fallan" in r["cand"]
    assert r["t1"] == "Orden enviada al bot. Pendiente de que la confirme."
    assert r["t2"].startswith("Hecho: Restaurada «ruido.example»") and r["t3"] == r["t2"]
    assert "El bot no ha confirmado el cambio de la fuente" in r["t4"]
    assert r["n"] == 2 and r["n2"] == 2
    assert r["sent"] == [{"type": "fuente", "accion": "restaurar", "id": "medio:ruido.example", "motivo": "Decidido a mano desde la web"},
                         {"type": "fuente", "accion": "silenciar", "id": "medio:canal.example", "motivo": "Decidido a mano desde la web"}]
    assert "ruido.example" in r["ruido"] and "agencia.example" not in r["ruido"]
    assert "viejo.example" in r["apagados"] and "ruido.example" in r["apagados"] and "agencia.example" not in r["apagados"]
    assert "Sin conexión con el bot" in r["t5"]
    _sin_null(head, html, r["chips"], r["cand"], r["ruido"], r["apagados"])


def test_evolucion_orden_web_y_volver_al_automatico():
    """«orden_web» (server.order_done): el rechazo de una fuente sale en el acto con su motivo (no el aviso genérico a
    los 10 s) y una orden aceptada que no cambia nada dice «Hecho»; lo viejo o de otra fuente no cuenta. Un peso fijado
    a mano tiene «Volver al automático» ({type:"peso_medio", peso:null}) y su resultado también llega por orden_web."""
    r = run(EVO + MEDIOS + """H.open(); reset();
      H.recv(evo(), apr({medios_mejores: [M("agencia.example", {peso: 1.3, peso_manual: 1.3, veredicto: "acierta"})]}), fvMsg());
      S.fvList = LISTA; S.tab = "evo"; evoBump(); renderAll(true);
      const html = H.el("#evoMedia").innerHTML, asked = []; confirm = m => (asked.push(m), true);
      H.fire("click", {"data-evo-aplicar": "p1"}); H.timeouts.length = 0;   // solo interesa el texto de su confirmación
      H.fire("click", {"data-fv-acc": "retirar", "data-fv-id": "medio:canal.example"});
      H.recv({type: "orden_web", ts: Date.now(), area: "fuentes", accion: "retirar", id: "medio:canal.example", ok: false, why: "No se puede: está retirada"});
      const t1 = H.el("#toast").textContent, p1 = S.fvPend;
      H.timeouts.filter(x => x.ms === 10000).forEach(x => x.f()); const t2 = H.el("#toast").textContent; H.timeouts.length = 0;
      H.fire("click", {"data-fv-acc": "silenciar", "data-fv-id": "medio:agencia.example"});
      H.recv({type: "orden_web", ts: Date.now() - 600000, area: "fuentes", accion: "silenciar", id: "medio:agencia.example", ok: true, why: null});
      H.recv({type: "orden_web", ts: Date.now(), area: "fuentes", accion: "silenciar", id: "medio:otra.example", ok: true, why: null});
      const sigue = !!S.fvPend;
      H.recv({type: "orden_web", ts: Date.now(), area: "fuentes", accion: "silenciar", id: "medio:agencia.example", ok: true, why: null});
      const t3 = H.el("#toast").textContent, p3 = S.fvPend;
      H.fire("click", {"data-apr-auto": "agencia.example"}); const t4 = H.el("#toast").textContent;
      H.recv({type: "orden_web", ts: Date.now(), area: "aprendizaje", accion: "peso", id: "agencia.example", ok: true, why: null});
      const t5 = H.el("#toast").textContent;
      H.fire("click", {"data-apr-auto": "agencia.example"});
      H.recv({type: "orden_web", ts: Date.now(), area: "aprendizaje", accion: "peso", id: "agencia.example", ok: false, why: "Falta el medio <b>"});
      const t6 = H.el("#toast").textContent;
      H.timeouts.filter(x => x.ms === 10000).forEach(x => x.f()); const t7 = H.el("#toast").textContent;
      return {html, asked, t1, p1, t2, sigue, t3, p3, t4, t5, t6, t7, sent: H.sent()};""")
    html = r["html"]
    assert 'data-apr-auto="agencia.example"' in html and "Volver al automático" in html and "fijado a mano" in html
    assert html.count("data-apr-auto=") == 1   # solo los fijados a mano
    assert "carga solo en ≤ 1 min" in r["asked"][0] and "reinici" not in r["asked"][0].replace("no hace falta reiniciar", "")
    assert r["t1"] == "No se pudo cambiar la fuente «canal.example»: No se puede: está retirada." and r["p1"] is None
    assert r["t2"] == r["t1"]   # respondió: no sale el aviso genérico
    assert r["sigue"] is True and r["t3"].startswith("Hecho: Silenciar «agencia.example»") and r["p3"] is None
    assert "peso automático" in r["asked"][-1] and r["t4"] == "Orden enviada al bot. Pendiente de que la confirme."
    assert r["t5"].startswith("Hecho: «agencia.example» vuelve al peso automático") and "solo paper" in r["t5"]
    assert r["t6"] == "No se pudo cambiar el peso de «agencia.example»: Falta el medio <b>." and r["t7"] == r["t6"]
    assert r["sent"][-2:] == [{"type": "peso_medio", "medio": "agencia.example", "peso": None,
                               "motivo": "Vuelve al automático desde la web"}] * 2
    assert r["sent"][1] == {"type": "fuente", "accion": "retirar", "id": "medio:canal.example",
                            "motivo": "Decidido a mano desde la web"}


def test_evolucion_lee_las_rutas_de_la_api():
    """Al abrir la pestaña en directo se leen /api/evolucion/historial, /informes, /api/aprendizaje(/noticias),
    /api/fuentes_vivas(/lista, /candidatos) y el detalle de un medio; lo que llega se pinta escapado."""
    r = run(EVO + MEDIOS + """H.open(); reset(); H.recv(evo(), fvMsg());
      const REPLY = [
        ["/api/evolucion/historial", {cambios: [{id: "v-old", ts: now - 20 * 86400e3, tipo: "fuente_añadida", sujeto: "nuevo.example", antes: null,
          despues: {estado: "prueba", peso: 0.5}, motivo: "Descubierto <b>", evidencia: {casos: 14, primeras: 4}, origen: "fuentes_vivas",
          revierte: null, etiqueta: "Medio añadido", revertida_por: null}], tipos: {fuente_nueva: "Tipo nuevo"}}],
        ["/api/evolucion/informes", {informes: [{id: "v1", ts: now - 7 * 86400e3, semana: "2026-W43", titulo: "Informe completo",
          resumen: "Resumen", secciones: [{titulo: "Qué ha funcionado", lineas: ["Con noticia a favor <b>: +0,62 R", 3]}], texto: "x"}]}],
        ["/api/evolucion", evo({ts: now - 1000, modo: "automatico"})],
        ["/api/aprendizaje/noticias?cuales=mejores", [{story: "s1", titular: "Titular <b>", url: "https://agencia.example/a?b=1&c=2", nombre: "Agencia",
          coin: "BTC", t0: now - 86400e3, dir: 1, r240: 1.5, explicacion: "Prevista alcista: acertó", operaciones: [{operacion: "o1", postmortem: "Salió por objetivo"}]}]],
        ["/api/aprendizaje/noticias?cuales=peores", [{story: "s2", titular: "Peor", url: "javascript:alert(1)", outlet: "canal.example", coin: "ETH",
          t0: now - 86400e3, dir: -1, r240: 2.1, explicacion: "Falló", operaciones: []}]],
        ["/api/aprendizaje/medio", {medio: "agencia.example", nombre: "Agencia", horizontes: {
          "15": {casos: 40, independientes: 12, p_aj: 0.55, ic90_aj: [0.45, 0.65], bruto: 0.6, base: 0.55, ret: 0.04},
          "60": {casos: 60, independientes: 22, p_aj: 0.6, ic90_aj: [0.52, 0.68], bruto: 0.7, base: 0.6, ret: 0.12}},
          ventanas: {casos: 10, independientes: 6, p_aj: 0.58, ic90_aj: [0.4, 0.7], bruto: 0.6, base: 0.5, ret: 0.3}}],
        ["/api/aprendizaje", {...apr(), medios: [M("lista.example", {peso: 1.3, veredicto: "acierta"})], postmortems: [], cambios: []}],
        ["/api/fuentes_vivas/lista", LISTA],
        ["/api/fuentes_vivas/candidatos", [{medio: "nuevo2.example", nombre: "Nuevo 2 <b>", region: "europa", lang: "es", estado: "verificado",
          estado_texto: "verificado, pendiente de alta", nota: "RSS verificado", rss: "https://nuevo2.example/rss", cifras: {casos: 12, acierto: 0.58},
          acciones: ["anadir"]}, {medio: "malo.example", estado: "bloqueado", estado_texto: "bloqueado", acciones: []}]]];
      H.fetchReply = url => { const x = REPLY.find(([p]) => url.startsWith(p)); return x ? {ok: true, json: async () => x[1]} : {ok: false}; };
      S.tab = "evo"; await loadEvo(); renderAll(true);
      const a = evoHtml(), modo = S.evo.modo;
      H.fire("click", {"data-evo-medio": "agencia.example"}); for (let i = 0; i < 20; i++) await Promise.resolve(); renderAll(true);
      const medio = H.el("#evoMedio").innerHTML, abierto = H.el("#evoMedio").hidden;
      H.fire("click", {"data-evo-medio": ""}); renderAll(true);
      H.fire("click", {"data-fv-acc": "anadir", "data-fv-id": "nuevo2.example"});
      return {a, modo, medio, abierto, cerrado: H.el("#evoMedio").hidden, fetches: H.fetches.slice(), sent: H.sent()};""")
    a, f = r["a"], r["fetches"]
    for p in ("/api/evolucion", "/api/evolucion/historial?limite=500&desde=", "/api/evolucion/informes?limite=1", "/api/aprendizaje",
              "/api/aprendizaje/noticias?cuales=mejores&horizonte=240&dias=30&limit=6", "/api/aprendizaje/noticias?cuales=peores",
              "/api/fuentes_vivas/lista?tipo=medio&limite=500", "/api/fuentes_vivas/candidatos?limite=40",
              "/api/aprendizaje/medio?outlet=agencia.example"):
        assert any(x.startswith(p) for x in f), p
    assert r["modo"] == "proponer"   # el bus (más nuevo) manda sobre la API
    tl = a["#evoTimeline"]
    assert "nuevo.example" in tl and "Descubierto &lt;b&gt;" in tl and "– → en prueba · peso 0,50" in tl and "Tipo nuevo" in a["#evoFilters"]
    assert "Informe completo" in a["#evoReport"] and "Qué ha funcionado" in a["#evoReport"] and "Con noticia a favor &lt;b&gt;: +0,62 R" in a["#evoReport"]
    best, worst = a["#evoBest"], a["#evoWorst"]
    assert '<a href="https://agencia.example/a?b=1&amp;c=2" target="_blank" rel="noopener noreferrer">Titular &lt;b&gt;</a>' in best
    assert "▲ prevista alcista" in best and "+1,50 %" in best and "Prevista alcista: acertó" in best and "Salió por objetivo" in best
    assert "javascript:" not in worst and "<b>Peor</b>" in worst and "▼ prevista bajista" in worst and "+2,10 %" in worst
    assert "lista.example" in a["#evoMedia"] and "×1,30" in a["#evoMedia"] and 'data-fv-id="medio:lista.example"' in a["#evoMedia"]
    cand = a["#evoCand"]
    assert "Nuevo 2 &lt;b&gt;" in cand and 'data-fv-acc="anadir" data-fv-id="nuevo2.example"' in cand and "verificado, pendiente de alta" in cand
    assert "malo.example" in cand and 'data-fv-id="malo.example"' not in cand and "acierto 58 %" in cand
    assert r["abierto"] is False and r["cerrado"] is True
    m = r["medio"]
    assert "+15 min" in m and "+1 h" in m and "+24 h" in m and "Sesión de apertura (4 h)" in m and "60 %" in m and "(52 % a 68 %)" in m
    assert "+0,12 %" in m and "cien titulares de la misma hora" in m
    assert r["sent"] == [{"type": "fuente", "accion": "anadir", "id": "nuevo2.example", "motivo": "Decidido a mano desde la web"}]
    _sin_null(*a.values(), m)


def test_evolucion_tolera_campos_ausentes_o_rotos():
    r = run(EVO + MEDIOS + """H.open(); reset(); S.tab = "evo";
      renderAll(true); const vacio = evoHtml();
      H.recv({type: "evolucion"}, {type: "aprendizaje"}, {type: "fuentes_vivas"}); renderAll(true); const casi = evoHtml();
      H.recv({type: "evolucion", ts: now + 1, estado: "rara", modo: 7, reglas: null, criterios: "x", campeon: {vivo: null, curva: "x", gemelo: 7,
                efectivos: {windows: "x", sl_pct: "3"}, degradacion: {evaluada: true, ic95: [null, 1]}, reglas: {ok: false, fallos: [null, "rompe"]}},
              aspirantes: [null, {frente: null, vivo: "x", curva: [{lunes: "x", R_acum: 1}]}, {frente: {faltan: "x", confirmacion: {estado: "rara"}}}],
              propuestas: [null, {id: 5, evidencia: [1, 2], bloqueada: {}}], recientes: "x", bloqueos: [null, {}],
              cambios: [null, {ts: "x", tipo: null, antes: [1, {}], despues: {a: {b: 1}}, evidencia: "x"}, {id: "z", tipo: "informe_semanal", despues: null}],
              informe: {}, ultima_orden: {ts: "x"}, proxima_evaluacion: 1e300},
             {type: "aprendizaje", estado: "error", global: {ic90: "x"}, medios_mejores: [null, {medio: null}, {medio: "a.b", ic90: [null, 1], acierto: "x"}]},
             {type: "fuentes_vivas", feeds: null, medios: "x", cambios: "x", revision: null});
      S.fvList = [null, {id: 5}, {id: "medio:a.b", tipo: "medio", estado: "rara", acciones: "x"}];
      S.fvCand = [null, {rss: "javascript:x", acciones: ["anadir"]}]; S.evoNews = {mejores: [null, {dir: "x", t0: "x", operaciones: "x"}], peores: "x"};
      S.evoInf = {secciones: [null, {lineas: "x"}]}; evoBump(); renderAll(true);
      return {vacio, casi, roto: evoHtml()};""")
    assert "aún no ha publicado su estado" in r["vacio"]["#evoHead"] and "Aún no hay medios medidos" in r["vacio"]["#evoMedia"]
    assert "Sin datos del campeón" in r["casi"]["#evoChamp"] and "sin estado" in r["casi"]["#evoHead"]
    roto = r["roto"]
    assert "rara" in roto["#evoHead"] and "a.b" in roto["#evoMedia"] and "javascript:" not in roto["#evoCand"]
    _sin_null(*r["vacio"].values(), *r["casi"].values(), *roto.values())


def test_evolucion_en_la_demostracion_es_un_ejemplo_marcado():
    """Sin bot, la pestaña enseña un estado de EJEMPLO rotulado en cada panel; los botones no envían nada."""
    r = run("""H.fire("click", {"data-tab": "evo"}); renderAll(true);
      const a = Object.fromEntries(["#evoHead", "#evoProps", "#evoChamp", "#evoAsp", "#evoTimeline", "#evoMediaHead", "#evoMedia", "#evoBest",
        "#evoWorst", "#evoCand", "#evoReport"].map(id => [id, H.el(id).innerHTML]));
      const note = H.el("#evoDemoNote").hidden, demo = H.el("#v-evo").classList.contains("demo"), badge = H.el("#evoBadge").hidden;
      H.fire("click", {"data-evo-aplicar": "p-demo-1"}); const t1 = H.el("#toast").textContent;
      H.fire("click", {"data-fv-acc": "silenciar", "data-fv-id": "medio:agencia-ejemplo.com"}); const t2 = H.el("#toast").textContent;
      H.fire("click", {"data-evo-medio": "agencia-ejemplo.com"}); renderAll(true);
      return {a, note, demo, badge, t1, t2, medio: H.el("#evoMedio").innerHTML, sockets: H.sockets.length};""", live=False)
    a = r["a"]
    assert r["note"] is False and r["demo"] is True and r["badge"] is True and r["sockets"] == 0
    assert "Ejemplo inventado" in a["#evoHead"] and "Solo paper" in a["#evoHead"]
    assert "Quitar la apertura de São Paulo" in a["#evoProps"] and "<svg" in a["#evoChamp"] and "E1-7d" in a["#evoAsp"]
    assert "ejemplo" in a["#evoTimeline"] and "agencia-ejemplo.com" in a["#evoMedia"] and "Ejemplo:" in a["#evoBest"]
    tl = a["#evoTimeline"]   # Revertir solo donde el bot lo ofrecería: ni el informe, ni el modo, ni lo ya revertido
    assert 'data-evo-revertir="v-demo-8"' in tl and 'data-evo-revertir="v-demo-9"' not in tl
    assert 'data-evo-revertir="v-demo-1"' not in tl and 'data-evo-revertir="v-demo-4"' not in tl
    assert "analisis-ejemplo.com" in a["#evoCand"] and "(ejemplo)" in a["#evoReport"]
    assert "Ejemplo: en la demostración no se envía nada" in r["t1"] and "Ejemplo: en la demostración no se envía nada" in r["t2"]
    assert "+15 min" in r["medio"]
    _sin_null(*a.values(), r["medio"])


def test_resumen_del_laboratorio_ligero_y_el_ejemplo_lo_usa():
    """scripts/incrustar_backtest.py deja de laboratorio.json solo lo ligero (sin operaciones ni vecinos) y el ejemplo de
    Evolución pone con él los nombres y cifras fuera de muestra reales, rotuladas como backtest del laboratorio."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("incrustar_backtest", HTML.parents[1] / "scripts" / "incrustar_backtest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    prom = {"id": "a|sig", "name": "Prometedora <b>", "family": "f", "class": "prometedora", "rules": {"sl_pct": 3},
            "is": {"trades": 57, "mean_R": 0.09, "win_rate": 0.45, "t": 1.2}, "oos": {"trades": 34, "mean_R": 0.2048, "win_rate": 0.5588},
            "trades_oos": [{"R": 1}] * 500, "neighbors": [{"id": "x"}] * 50}
    lab = {"generated": 1, "period": {"from": "2023-11-01"}, "frozen": {"date": "2026-10-26", "strategies": 2000},
           "summary": {"tested": 2000, "promising": 2, "robust": 0, "expected_by_chance": 9, "otro": 1},
           "promising": [prom, {**prom, "id": "b|sig", "name": "Segunda"}], "robust": [], "all": [prom] * 100}
    s = mod.lab_summary(lab)
    assert s["summary"] == {"tested": 2000, "promising": 2, "robust": 0, "expected_by_chance": 9} and s["frozen"] == "2026-10-26"
    assert s["promising"][0] == {"id": "a|sig", "name": "Prometedora <b>", "family": "f", "class": "prometedora", "rules": {"sl_pct": 3},
                                 "is": {"trades": 57, "mean_R": 0.09, "win_rate": 0.45}, "oos": {"trades": 34, "mean_R": 0.2048, "win_rate": 0.5588}}
    assert len(json.dumps(s)) < 3000 and "trades_oos" not in json.dumps(s)
    page = HTML.read_text(encoding="utf-8")
    if 'id="labData"' in page:   # el que ya está incrustado en la web se lee y es ligero
        blob = page.split('<script type="application/json" id="labData">', 1)[1].split("</script>", 1)[0]
        assert len(blob) < 20_000 and isinstance(json.loads(blob)["promising"], list)
    r = run(f"""S.lab = {json.dumps(s)}; demoEvo(); S.tab = "evo"; renderAll(true);
      return {{head: H.el("#evoHead").innerHTML, asp: H.el("#evoAsp").innerHTML}};""", live=False)
    assert "El laboratorio real probó 2000 estrategias y dejó 2 prometedoras y 0 robustas" in r["head"]
    assert "Prometedora &lt;b&gt;" in r["asp"] and "backtest del laboratorio: 34 op. fuera de muestra, R medio +0,20 R, 56 % ganadoras" in r["asp"]
    _sin_null(r["head"], r["asp"])


def test_backtest_muestra_la_estrategia_en_marcha_y_el_laboratorio():
    """La pestaña Backtest abre con lo que opera el motor (E2, 7 días) y el laboratorio, con sus advertencias; el motor v2
    queda rotulado como anterior. En la demostración usa los datos reales incrustados (impulsoData y labData)."""
    page = HTML.read_text(encoding="utf-8")
    assert "Motor anterior (v2 «ruptura»): ya no opera" in page
    imp = {"generated": 1793150000000, "split": 1755295200000, "nota": "Variante elegida <b>después</b>",
           "fuera": {"n": 202, "mean_R": 0.3459, "win_rate": 0.41, "salidas": {"stop": 113, "objetivo": 60, "tiempo": 29},
                     "horas_media": 64.9, "largos": {"n": 85, "mean_R": 0.098}, "cortos": {"n": 117, "mean_R": 0.526},
                     "desde": 1755655200000, "hasta": 1790778780000, "total_R": 69.88, "max_dd_R": 12.3,
                     "curva_R": [[1755655200000, 0.5], [1790778780000, 69.88]]},
           "dentro": {"n": 469, "mean_R": 0.1297, "win_rate": 0.33, "desde": 1698845400000, "total_R": 60.8,
                      "curva_R": [[1698845400000, -1], [1755481500000, 60.8]]}}
    lab = {"summary": {"tested": 2000, "promising": 2, "robust": 0, "expected_by_chance": 9, "beat_base": 68, "base_tested": 1358,
                       "expected_by_chance_all": 68}, "frozen": "2026-10-26",
           "period": {"from": "2023-11-01", "to": "2026-10-23"},
           "promising": [{"id": "ap_mom:15:0,2|x", "name": "E2 <i>", "class": "prometedora", "is": {"trades": 557, "mean_R": 0.0436, "win_rate": 0.41},
                          "oos": {"trades": 278, "mean_R": 0.1402, "win_rate": 0.52}}],
           "avance": {"days": 668, "trades": 1356, "total_pct": -61.17, "max_dd_pct": 65.28, "top": 10}}
    r = run(f"""S.btImp = {{impulso: {json.dumps(imp)}, lab: {json.dumps(lab)}}}; S.tab = "bt"; renderAll(true);
      const sinMotor = H.el("#btLab").innerHTML;
      S.openings = {{strategy: "impulso", params: {{preset: "impulso"}}, windows: []}}; renderAll(true);
      const a = {{sinMotor, live: H.el("#btLive").innerHTML + H.el("#btLiveKpis").innerHTML, lab: H.el("#btLab").innerHTML + H.el("#btLabKpis").innerHTML}};
      S.openings = null;
      S.btImp = {{impulso: null, lab: null}}; renderBtLive();
      return {{...a, vacio: H.el("#btLive").innerHTML, vacioLab: H.el("#btLab").innerHTML}};""", live=False)
    live, lb = r["live"], r["lab"]
    assert "+0,35 R" in live and "202 op." in live and "+69,88 R" in live and "≈ +34,9 % de la cuenta" in live
    assert "No es una ventaja demostrada" in live and "+0,14 R en 278 operaciones" in live
    assert "La cuenta honesta en avance pierde" in live and "-61,2 %" in live and "65,3 %" in live
    assert "+0,53 R" in live and "+0,10 R" in live and "Variante elegida &lt;b&gt;después&lt;/b&gt;" in live
    assert "E2 &lt;i&gt;" in lb and "la opera el motor" in lb and "<small>Probadas</small><b>2000" in lb and "lista congelada el 2026-10-26" in lb
    assert "la opera el motor" not in r["sinMotor"]   # sin saber qué opera el motor no se dice que es E2
    assert "<b>68 de 1358</b><span>por puro azar se esperan unas 68" in lb
    assert "no trae el backtest del motor" in r["vacio"] and "no trae el laboratorio" in r["vacioLab"]
    _sin_null(live, lb)
    if 'id="impulsoData"' in page and 'id="labData"' in page:   # el incrustado real también se pinta sin huecos
        blob = lambda i: page.split(f'<script type="application/json" id="{i}">', 1)[1].split("</script>", 1)[0]
        r2 = run(f"""S.btImp = {{impulso: {blob("impulsoData")}, lab: {blob("labData")}}}; S.tab = "bt"; renderAll(true);
          return {{live: H.el("#btLive").innerHTML + H.el("#btLiveKpis").innerHTML, lab: H.el("#btLab").innerHTML + H.el("#btLabKpis").innerHTML}};""", live=False)
        assert "Fuera de muestra" in r2["live"] and "Probadas" in r2["lab"]
        _sin_null(r2["live"], r2["lab"])


def test_resultados_muestran_la_cartera_de_prueba_en_euros():
    """La cartera de prueba (scripts/nueva_cartera_paper.py) se ve con su nombre y su equivalente en euros."""
    r = run("""H.open(); reset();
      H.recv(pf({equity: 111.37, start_equity: 111.37, cuenta: {nombre: "Cartera de prueba de 100 € <x>", eur: 100, eur_usdc: 1.1137, usdc: 111.37}}));
      S.tab = "results"; renderAll(true); renderResults(); return {k: H.el("#resKpis").innerHTML};""")
    assert "≈ 100,00 €" in r["k"] and "Cartera de prueba de 100 € &lt;x&gt; (ficticia)" in r["k"]
    _sin_null(r["k"])


# Mensaje «cripto_aperturas» del bot (contrato 28-10-2026): cómo reacciona la cripto en la apertura de cada bolsa.
CRIPTO_AP = r"""
const MIN = 60e3, HORA = 3600e3;
const coinsNow = () => ({BTC: {ret_pct: 0.42, range_pct: 1.1, high_pct: 0.8, low_pct: -0.3, spark: [[now - 60*MIN, 0], [now - 30*MIN, 0.2], [now - MIN, 0.42]]},
  ETH: {ret_pct: -0.2, range_pct: 1.85, high_pct: 0.9, low_pct: -0.95, spark: [[now - 60*MIN, 0], [now - MIN, -0.2]]},
  SOL: {ret_pct: 1.3, range_pct: 2.4, high_pct: 1.5, low_pct: -0.9, spark: []},
  XRP: {ret_pct: null, range_pct: null, high_pct: null, low_pct: null, spark: null}});
const hist = {n: 377, desde: "2023-11-01", any_range_ge2_rate: 0.76, any_range_ge2_8h_rate: 0.93, coins: {
  BTC: {up_rate: 0.5, mean_ret_pct: 0.02, mean_range_pct: 1.6, range_ge2_rate: 0.3, range_ge2_8h_rate: 0.53},
  ETH: {up_rate: 0.49, mean_ret_pct: 0, mean_range_pct: 2.1, range_ge2_rate: 0.5, range_ge2_8h_rate: 0.77},
  SOL: {up_rate: 0.51, mean_ret_pct: 0.1, mean_range_pct: 2.6, range_ge2_rate: 0.69, range_ge2_8h_rate: 0.89},
  XRP: {up_rate: null, mean_ret_pct: null, mean_range_pct: null, range_ge2_rate: null, range_ge2_8h_rate: null}}};
const ca = (mk = {}, o = {}) => ({type: "cripto_aperturas", ts: now, coins: ["BTC", "ETH", "SOL", "XRP"], markets: {
  tokio: {name: "Tokio", window: "tokio-seul", state: "en_curso", open_ms: now - 70*MIN, next_open_ms: now + 23*HORA, minutes_since_open: 70,
          coins: coinsNow(), last: null, hist},
  seul: {name: "Seúl", window: "tokio-seul", state: "en_curso", open_ms: now - 70*MIN, next_open_ms: null, minutes_since_open: null,
         coins: null, last: null, hist: {n: null, desde: null, coins: null}},
  londres: {name: "Londres", window: "europa", state: "terminada", open_ms: now - 5*HORA, next_open_ms: now + 19*HORA, minutes_since_open: 300,
            coins: {BTC: {ret_pct: -0.8, range_pct: 2.2, high_pct: 0.4, low_pct: -1.8, spark: [[now - 5*HORA, 0], [now - HORA, -0.8]]}}, last: null, hist},
  nuevayork: {name: "Nueva York", window: "nuevayork", state: "por_abrir", open_ms: now - 30*HORA, next_open_ms: now + 2*HORA, minutes_since_open: null,
              coins: null, last: {open_ms: now - 30*HORA, coins: {BTC: {ret_4h_pct: 0.6, range_4h_pct: 1.9}, ETH: {ret_4h_pct: 1.2, range_4h_pct: 2.5}}}, hist},
  dubai: {name: "Dubái <img src=x onerror=alert(1)>", window: null, state: "por_abrir", open_ms: null, next_open_ms: null, minutes_since_open: null,
          coins: null, last: null, hist: {n: 0, desde: null, coins: null, nota: "Sin calendario de festivos <i>"}},
  ...mk},
  chain: {after_up_rate: 0.49, after_down_rate: 0.511, base_rate: 0.501, n: 7464, nota: "Medido sin mirar el futuro <b>"},
  total: {...hist, n: 3771}, ...o});
const INDICE = {tokio: {name: "Tokio", index: "Nikkei 225", state: "abierta", change_pct: 5.55, last_session: {change_pct: -7.77}, spark: [1, 2], price: 62693.16},
  londres: {name: "Londres", index: "FTSE 100", state: "cerrada", change_pct: 3.33, last_session: {change_pct: 3.33}, futures: {change_pct: 4.44}}};
"""
SIN_INDICES = ("Nikkei", "KOSPI", "DAX", "FTSE", "IBEX", "S&amp;P", "Hang Seng", "Ibovespa", "SSE Composite", "DFM", "5,55", "7,77", "3,33",
               "4,44", "62.693")


def _sin_indices(*htmls, solo_nombres=False):
    """Ni los nombres de los índices ni (con el escenario INDICE) sus cifras. En la demostración solo los nombres: sus
    precios son aleatorios y un «rango 4,44 %» de la cripto puede salir por azar."""
    bads = SIN_INDICES[:10] if solo_nombres else SIN_INDICES
    for html in htmls:
        for bad in bads:
            assert bad not in html, bad


def test_paneles_de_bolsas_rotulados_como_cripto():
    """Los paneles que molestaban ya no hablan de índices: Mercado, Predicciones y Mando se rotulan como cripto."""
    page = HTML.read_text(encoding="utf-8")
    assert "Bolsas del mundo: cómo reacciona la cripto en cada apertura" in page and "Cripto en las próximas aperturas" in page
    assert "Bolsas · próximas sesiones" not in page and "Índice de cada bolsa" not in page
    assert "Probabilidad de que la sesión cierre por encima de su apertura" not in page


def test_bolsas_del_mundo_ensenan_la_cripto_y_no_el_indice():
    """Mercado y aperturas: cada tarjeta enseña BTC, ETH, SOL y XRP en la apertura de esa bolsa según su estado (en curso,
    terminada, por abrir), con su minicurva y lo histórico; sin datos avisa, nunca pinta el índice ni huecos."""
    r = run(CRIPTO_AP + """H.open(); reset();
      H.recv({type: "trend", ts: now, markets: INDICE});
      renderWorldMarkets(); const vacio = H.el("#mkTiles").innerHTML, note0 = H.el("#mkNote").hidden;
      H.recv({type: "cripto_aperturas", ts: null, estado: "arrancando", coins: ["BTC"], markets: {}, chain: null});
      renderWorldMarkets(); const arr = H.el("#mkNote").textContent, arrTiles = H.el("#mkTiles").innerHTML;
      H.recv(ca()); renderWorldMarkets(); const html = H.el("#mkTiles").innerHTML;
      const part = name => html.split('<div class="mtile ca">').find(x => x.includes("<b>" + name + "</b>")) || "";
      return {vacio, note0, arr, arrTiles, html, tokio: part("Tokio"), seul: part("Seúl"), londres: part("Londres"), ny: part("Nueva York"),
              dubai: part("Dubái"), madrid: part("Madrid"), note: H.el("#mkNote").hidden, noteTxt: H.el("#mkNote").textContent};""")
    assert r["vacio"].count("Aún sin datos de la cripto en esta apertura") == 10 and r["note0"] is True
    assert "El bot está calculando lo histórico" in r["arr"] and r["arrTiles"].count("Aún sin datos de la cripto en esta apertura") == 10
    assert ("Todas las aperturas juntas (3771 desde el 01-11-2023): rango ≥ 2 % en 4 h: BTC 30 % · ETH 50 % · SOL 69 % · XRP – · "
            "alguna de las 4 76 %; en 8 h: BTC 53 % · ETH 77 % · SOL 89 % · XRP – · alguna de las 4 93 %.") in r["noteTxt"].replace("3.771", "3771")
    assert "Que se mueva no dice hacia dónde: sube en 4 h BTC 50 %" in r["noteTxt"] and r["note"] is False
    t = r["tokio"]
    assert "en curso" in t and "Desde la apertura" in t and "<b>BTC</b>" in t and "+0,42 %" in t and "rango 1,10 %" in t
    assert "-0,20 %" in t and "rango 2,40 %" in t and 'title="máximo +0,80 % · mínimo -0,30 %"' in t
    assert re.search(r"quedan 2 h (49|50) min de las 4 h", t) and t.count("<path") == 2   # minicurvas de BTC y ETH (SOL y XRP sin puntos)
    assert "En esta apertura: rango ≥ 2 % en 4 h el 69 % de las veces (SOL) · sube 51 %" in t
    assert "<small>≥ 2 % en 8 h</small><b>53 %</b><b>77 %</b><b>89 %</b><b>–</b>" in t
    assert "377 aperturas desde el 01-11-2023" in t and "no una previsión" in t
    assert "Alguna de las 4 con rango ≥ 2 %: 76 % en 4 h · 93 % en 8 h" in t
    assert "<b>XRP</b><b class=\"\">–</b>" in t and "rango –" in t
    lo = r["londres"]
    assert "terminada" in lo and "Resultado a las 4 h" in lo and "-0,80 %" in lo and "rango 2,20 %" in lo and "próxima en 1" in lo
    ny = r["ny"]
    assert "por abrir" in ny and "Su última apertura" in ny and "+0,60 %" in ny and "rango 1,90 %" in ny and "+1,20 %" in ny
    assert re.search(r"abre en (1 h 59|2 h 00) min", ny)
    assert "Aún sin datos de su última apertura" in r["dubai"] and "Sin histórico de la cripto en esta apertura" in r["dubai"]
    assert "sin próxima sesión conocida" in r["dubai"] and "img" not in r["html"] and "Sin calendario de festivos &lt;i&gt;" in r["dubai"]
    assert "Aún sin datos de la cripto en esta apertura" in r["seul"] and "Sin histórico" in r["seul"]
    assert "Aún sin datos de la cripto en esta apertura" in r["madrid"]
    _sin_indices(r["vacio"], r["html"], r["noteTxt"])
    _sin_null(r["vacio"], r["html"], r["noteTxt"], r["arr"])


def test_mundo_y_mando_pintan_la_reaccion_de_la_cripto():
    """La barra verde y roja de cada ciudad (vista Mundo) y la lista de bolsas de Mando usan la media de las 4 criptos en
    esa apertura (discontinua: su apertura anterior), no el índice ni su previsión."""
    r = run(CRIPTO_AP + """H.open(); reset();
      H.recv({type: "trend", ts: now, markets: INDICE});
      H.recv({type: "predictions", ts: now, items: [{id: "tokio-x", kind: "bolsa", target: "tokio", label: "Tokio (Nikkei 225)", event: "apertura",
              when_utc: now + 3600e3, horizon: "sesión", prob_up: 0.9, expected_pct: 1, confidence: "alta", drivers: []}], accuracy: {}});
      const before = World.marketInfo(MKBY.tokio, {open: true, next: null}), lineBefore = marketLine(MKBY.tokio);
      H.recv(ca());
      const info = k => World.marketInfo(MKBY[k], {open: false, next: now + 3600e3});
      S.tab = "mando"; renderMando();
      return {before, lineBefore, tokio: info("tokio"), londres: info("londres"), ny: info("nuevayork"), dubai: info("dubai"), madrid: info("madrid"),
              lines: MK.map(m => marketLine(m).txt), md: H.el("#mdMarkets").innerHTML, pred: H.el("#mdPred").innerHTML};""")
    assert r["before"].get("val") is None and "5,55" not in r["before"]["sub"] and "aún sin datos de la cripto" in r["lineBefore"]["txt"]
    assert r["tokio"]["sub"].startswith("cripto +0,51 %") and abs(r["tokio"]["val"] - 0.2533) < 0.01 and not r["tokio"].get("pred")
    assert r["londres"]["sub"] == "cripto -0,80 % a las 4 h" and abs(r["londres"]["val"] + 0.4) < 1e-9
    assert r["ny"]["sub"].startswith("última: cripto +0,90 %") and r["ny"]["pred"] is True
    assert r["dubai"].get("val") is None and r["madrid"].get("val") is None
    assert r["lines"][0].startswith("en curso · cripto +0,51 %") and "quedan" in r["lines"][0]
    assert any(x.startswith("última apertura: cripto +0,90 %") for x in r["lines"])
    _sin_indices(r["md"], r["pred"], " ".join(r["lines"]), r["tokio"]["sub"], r["londres"]["sub"])
    _sin_null(r["md"], r["pred"])


def test_predicciones_cripto_en_las_proximas_aperturas():
    """Predicciones: «Cripto en las próximas aperturas» con la probabilidad histórica de subir y de un rango ≥ 2 % por
    moneda, la nota honesta del encadenamiento (no predice) y, en «próxima ventana», los factores del bot sin índices."""
    r = run(CRIPTO_AP + """H.open(); reset();
      S.tab = "pred"; renderAll(true); const vacio = H.el("#predMk").innerHTML, chain0 = H.el("#predChain").innerHTML;
      H.recv({type: "predictions", ts: now, items: [
        {id: "tokio-x", kind: "bolsa", target: "tokio", label: "Tokio (Nikkei 225)", event: "apertura", when_utc: now + 3600e3, horizon: "sesión",
         prob_up: 0.9, expected_pct: 1, confidence: "alta", drivers: [{factor: "Hong Kong cerró −0,28 %", effect_pp: -6.3, source: "encadenamiento"}]},
        {id: "BTC-1h", kind: "cripto", target: "BTC", label: "BTC · próxima hora", event: "ventana", when_utc: now, horizon: "1h", prob_up: 0.55,
         expected_pct: 0.1, confidence: "baja", drivers: [{factor: "Flujo de órdenes <b>", effect_pp: 2.5, source: "libro"}]}],
        accuracy: {evaluated: 10, hit_rate: 0.6, by_kind: {bolsa: {evaluated: 5, hit_rate: 0.8}, cripto: {evaluated: 5, hit_rate: 0.4}}}});
      H.recv(ca()); renderPred();
      const a = {mk: H.el("#predMk").innerHTML, chain: H.el("#predChain").innerHTML, cr: H.el("#predCr").innerHTML, kpi: H.el("#predKpis").innerHTML};
      H.recv(ca({}, {chain: null})); renderPred();
      return {...a, vacio, chain0, chainNull: H.el("#predChain").innerHTML};""")
    mk, ch = r["mk"], r["chain"]
    assert "Aún sin datos de la cripto en las aperturas" in r["vacio"] and r["chain0"] == ""
    assert mk.index("Tokio") < mk.index("Nueva York") < mk.index("Londres") and "Dubái" not in mk
    assert "Sube en 4 h" in mk and "Rango ≥ 2 % en 4 h" in mk and "<b>69 %</b>" in mk and 'class="pos">51 %</b>' in mk and 'class="neg">49 %</b>' in mk
    assert 'aria-label="sin datos"' in mk and "1.0000" not in mk
    assert "A 8 h el rango ≥ 2 % llega a: BTC 53 % · ETH 77 % · SOL 89 %." in mk and "377 aperturas desde el 01-11-2023" in mk
    assert "Frecuencia pasada, no una previsión" in mk and "En curso: abrió" in mk and "Apertura " in mk
    assert "Tras una apertura en positivo, la siguiente sube el 49 % de las veces: no predice." in ch
    assert "Tras una en negativo, el 51 %; de media, el 50 %" in ch and "casos)" in ch and ("7464" in ch or "7.464" in ch)
    assert "espejismo del solape" in ch and "Medido sin mirar el futuro &lt;b&gt;" in ch
    assert "aún sin datos" in r["chainNull"]
    assert "BTC · próxima hora" in r["cr"] and "Flujo de órdenes &lt;b&gt;" in r["cr"]
    k = r["kpi"]
    assert "Predicciones cripto</small><b>1</b>" in k and "Acierto en cripto</small><b>40 %</b>" in k and "Acierto en bolsas" not in k
    assert "<small>Próxima apertura</small><b>Nueva York</b>" in k and "Tras una en positivo</small><b>49 %</b>" in k and "no predice" in k
    _sin_indices(mk, ch, r["cr"], k)
    assert "Hong Kong cerró" not in mk + r["cr"]
    _sin_null(mk, ch, r["cr"], k, r["chainNull"], r["vacio"])


def test_cripto_aperturas_y_reflejo_toleran_campos_rotos():
    """Campos ausentes, nulos o de otro tipo en «cripto_aperturas» y en el reflejo no rompen ninguna pestaña ni dejan huecos."""
    r = run(CRIPTO_AP + """H.open(); reset();
      const out = [];
      const pinta = () => { renderWorldMarkets(); renderPred(); S.tab = "mando"; renderMando();
        out.push(H.el("#mkTiles").innerHTML, H.el("#predMk").innerHTML, H.el("#predChain").innerHTML, H.el("#predKpis").innerHTML, H.el("#mdMarkets").innerHTML,
                 MK.map(m => World.marketInfo(m, {open: true, next: null}).sub).join(" | ")); };
      H.recv({type: "cripto_aperturas", ts: now, markets: null, chain: "x"}); pinta();
      H.recv({type: "cripto_aperturas", ts: now, markets: {tokio: "x", seul: {state: "<b>raro</b>", open_ms: "ayer", next_open_ms: now + HORA,
        coins: {BTC: "x", ETH: {ret_pct: "1", range_pct: {}, spark: [[1], "a", [null, 2], [now, "3"]]}}, hist: {n: "mucho", desde: 5, coins: {BTC: {up_rate: "0.5"}, ETH: null}}},
        londres: {state: "terminada", open_ms: now - 5*HORA, coins: [], last: "x", hist: []},
        nuevayork: {state: "por_abrir", next_open_ms: now + HORA, last: {open_ms: "x", coins: {BTC: {ret_4h_pct: null}}}, hist: {coins: {}}}},
        chain: {after_up_rate: "49 %", after_down_rate: null, n: "x", nota: 5}}); pinta();
      H.recv(ev("captured", "z1", {headline: "Macro rara", src: "AP", tier: "T1", t0: now, coins: []}),
             ev("triaged", "z1", {triage: {tipo: "macro", importancia: 7, credibilidad: 80, sentimiento: "alcista", repercusion: {direccion: 60, magnitud: 70, activos: []}}}),
             ev("reflejo", "z1", {reflejo: {estado: "rara", mov_pct: "x", min: null, ref: null}}),
             ev("captured", "z2", {headline: "Reflejo de otro tipo", src: "AP", tier: "T1", t0: now, coins: []}),
             ev("triaged", "z2", {triage: {tipo: "macro", importancia: 7, credibilidad: 80, sentimiento: "bajista", repercusion: {direccion: -60, magnitud: 70, activos: []}}}),
             ev("reflejo", "z2", {reflejo: "x"}), ev("reflejo", "z3", null));
      const nb = newsBalance(Date.now());
      S.sel = "z1"; S.tab = "live"; renderTrace(); renderFeed(); out.push(H.el("#trace").innerHTML, H.el("#feed").innerHTML);
      S.sel = "z3"; renderTrace(); out.push(H.el("#trace").innerHTML);
      H.fire("click", {"data-tab": "world"}); World.panes(); out.push(H.el("#wRight").innerHTML);
      return {out, items: nb.items.map(x => x.st.id), wait: nb.waiting.map(x => x.st.id)};""")
    assert r["items"] == ["z2"] and r["wait"] == ["z1"]
    assert "Reflejo sin estado" in r["out"][-4] and "estado del reflejo desconocido" in r["out"][-4]
    assert "<b>raro</b>" not in "".join(r["out"])
    _sin_indices(*r["out"])
    _sin_null(*r["out"])


def test_cripto_aperturas_por_la_api_si_no_llega_por_el_websocket():
    """Al conectar se pide GET /api/cripto_aperturas; un bot sin esa ruta (404) no rompe nada y se espera al mensaje."""
    r = run(CRIPTO_AP + """H.fetchReply = url => url === "/api/cripto_aperturas" ? {ok: true, json: async () => ca()} : {ok: false};
      H.open(); reset(); await loadApi();
      const a = {has: !!(S.criptoAp && S.criptoAp.markets.tokio), f: H.fetches.slice()};
      H.fetchReply = null; reset(); await loadApi();
      return {...a, none: S.criptoAp === null};""")
    assert r["has"] and "/api/cripto_aperturas" in r["f"] and r["none"]


def test_reflejo_de_noticias_macro_en_la_balanza_y_el_expediente():
    """Evento «reflejo»: una noticia macro o geopolítica solo pesa en la balanza y en la señal de Mundo si la cripto la
    confirma; pendiente, contraria o sin reflejo se enseñan aparte con su etiqueta. Las noticias cripto cuentan como siempre."""
    r = run("""H.open(); reset();
      const tr = dir => ({tipo: "macro", importancia: 7, credibilidad: 80, sentimiento: dir > 0 ? "alcista" : "bajista",
        repercusion: {direccion: 60*dir, magnitud: 70, activos: ["BTC", "ETH", "SOL", "XRP"]}, monedas: ["BTC"]});
      H.recv(ev("captured", "m1", {headline: "La Fed recorta <tipos>", src: "Reuters", tier: "T1", t0: now, coins: []}), ev("triaged", "m1", {triage: tr(1)}),
             ev("reflejo", "m1", {reflejo: {estado: "pendiente", mov_pct: 0.1, min: 5, ref: "media"}}, {agent: "mercado"}));
      H.recv(ev("captured", "m2", {headline: "Guerra comercial", src: "AP", tier: "T1", t0: now, coins: []}), ev("triaged", "m2", {triage: tr(-1)}),
             ev("reflejo", "m2", {reflejo: {estado: "contraria", mov_pct: 0.7, min: 22, ref: "media"}}));
      H.recv(ev("captured", "m3", {headline: "PIB de la eurozona", src: "AP", tier: "T1", t0: now, coins: []}), ev("triaged", "m3", {triage: tr(1)}),
             ev("reflejo", "m3", {reflejo: {estado: "sin_reflejo", mov_pct: 0.2, min: 60, ref: "BTC"}}));
      H.recv(ev("captured", "c1", {headline: "ETF de SOL aprobado", src: "CoinDesk", tier: "T2", t0: now, coins: ["SOL"]}),
             ev("triaged", "c1", {triage: {...tr(1), tipo: "regulatorio", repercusion: {direccion: 60, magnitud: 70, activos: ["SOL"]}}}));
      const t = () => Date.now(), nb1 = newsBalance(t()), s1 = coinSignal("BTC", nb1, t());
      H.fire("click", {"data-tab": "world"}); World.panes(); const right1 = H.el("#wRight").innerHTML;
      S.tab = "live"; S.sel = "m1"; renderTrace(); const tr1 = H.el("#trace").innerHTML;
      renderFeed(); const feed1 = H.el("#feed").innerHTML;
      H.recv(ev("reflejo", "m1", {reflejo: {estado: "confirmada", mov_pct: 0.62, min: 18, ref: "media"}}));
      const nb2 = newsBalance(t()), s2 = coinSignal("BTC", nb2, t());
      S.tab = "world"; World.panes(); const right2 = H.el("#wRight").innerHTML;
      S.tab = "live"; renderTrace(); const tr2 = H.el("#trace").innerHTML;
      S.sel = "m2"; renderTrace(); const tr3 = H.el("#trace").innerHTML;
      S.sel = "m3"; renderTrace(); const tr4 = H.el("#trace").innerHTML;
      renderFeed();
      const ids = a => a.map(x => x.st.id).sort();
      return {items1: ids(nb1.items), wait1: ids(nb1.waiting), btc1: nb1.by.BTC.n, items2: ids(nb2.items), wait2: ids(nb2.waiting), btc2: nb2.by.BTC.n,
              why1: s1.why[0], why2: s2.why[0], tilt1: newsTilt(S.stories.get("m2"), t()), right1, right2, tr1, tr2, tr3, tr4, feed1,
              feed2: H.el("#feed").innerHTML, agent: S.events.find(e => e.kind === "reflejo").agent,
              tones: S.events.filter(e => e.kind === "reflejo").map(e => e.tone), final: S.stories.get("m1").final || null, counts: S.counts.reflejo};""")
    assert r["items1"] == ["c1"] and r["wait1"] == ["m1", "m2", "m3"] and r["btc1"] == 0
    assert r["items2"] == ["c1", "m1"] and r["wait2"] == ["m2", "m3"] and r["btc2"] == 1
    assert r["why1"].startswith("Sin noticias que inclinen BTC") and r["why2"].startswith("Noticias: ▲") and r["tilt1"] is None
    assert "Macro y geopolítica que la cripto no confirma · no cuentan" in r["right1"] and "Esperando al mercado" in r["right1"]
    assert "La cripto dice lo contrario" in r["right1"] and "Sin reflejo en la cripto" in r["right1"] and "La Fed recorta &lt;tipos&gt;" in r["right1"]
    assert "La cripto lo confirma · +0,62 %" in r["right2"]
    assert "Reflejo en la cripto" in r["tr1"] and "Esperando al mercado" in r["tr1"] and "No cuenta todavía" in r["tr1"] and "5 min de 60" in r["tr1"]
    assert "La cripto lo confirma" in r["tr2"] and "Cuenta en la señal" in r["tr2"] and "+0,62 %" in r["tr2"]
    assert "La cripto dice lo contrario" in r["tr3"] and "se movió en contra" in r["tr3"]
    assert "Sin reflejo en la cripto" in r["tr4"] and "BTC (faltan datos de las demás)" in r["tr4"]
    assert "Esperando al mercado" in r["feed1"] and "La cripto dice lo contrario" in r["feed1"] and "La cripto lo confirma" in r["feed2"]
    assert r["agent"] == "flow" and r["tones"] == ["warn", "bad", "muted", "good"] and r["final"] is None and r["counts"] == 4
    _sin_null(r["right1"], r["right2"], r["tr1"], r["tr2"], r["tr3"], r["tr4"], r["feed1"], r["feed2"])


def test_demostracion_cripto_aperturas_y_reflejo_simulados():
    """La demostración genera un «cripto_aperturas» con los precios simulados (marcado como simulación) y mide el reflejo
    de las noticias macro igual que el bot; todas las pestañas se pintan sin índices ni huecos."""
    r = run("""const A = S.criptoAp, ms = Object.values(A.markets);
      S.tab = "market"; renderAll(true); const mk = H.el("#mkTiles").innerHTML, note = H.el("#mkNote");
      S.tab = "pred"; renderAll(true); const pred = H.el("#predMk").innerHTML + H.el("#predChain").innerHTML + H.el("#predKpis").innerHTML;
      S.tab = "mando"; renderAll(true); const md = H.el("#mdMarkets").innerHTML + H.el("#mdPred").innerHTML;
      H.fire("click", {"data-tab": "world"}); World.panes(); const right = H.el("#wRight").innerHTML;
      const info = MK.map(m => World.marketInfo(m, {open: false, next: S.vnow + 7200e3}));
      // una noticia macro alcista: la cripto sube un 1 % y el reflejo la confirma al minuto siguiente
      emit({kind: "captured", story: "dm1", title: "La Fed recorta tipos", coins: [],
            patch: {headline: "La Fed recorta tipos", src: "Fed", tier: "T0", t0: S.vnow, px0: {...S.prices}}});
      simReflejo("dm1", 1); const r0 = S.stories.get("dm1").reflejo.estado;
      const um = S.stories.get("dm1").reflejo.umbral_pct;   // el mayor de 0,5 % y 2σ de 60 min, como el bot
      for (const c of COINS) S.prices[c] *= 1 + Math.max(1, um + 0.3)/100;
      S.vnow += 61000; S.queue.sort((a, b) => a.at - b.at); while (S.queue.length && S.queue[0].at <= S.vnow) S.queue.shift().fn();
      const r1 = S.stories.get("dm1").reflejo;
      return {demo: A.demo, n: ms.length, states: ms.map(x => x.state), keys: Object.keys(A.markets), chain: A.chain, mk, note: note.hidden,
              noteTxt: note.textContent, pred, md, right, info, r0, r1, sent: H.sockets.length};""", live=False)
    assert r["demo"] is True and r["n"] == 10 and set(r["states"]) <= {"por_abrir", "en_curso", "terminada"}
    assert set(r["keys"]) == {"tokio", "seul", "shanghai", "hongkong", "dubai", "frankfurt", "londres", "madrid", "nuevayork", "saopaulo"}
    assert r["chain"]["after_up_rate"] == 0.49 and r["note"] is False and "Simulación" in r["noteTxt"]
    assert "En esta apertura: rango ≥ 2 % en 4 h el 69 % de las veces (SOL)" in r["mk"]
    assert "Tras una apertura en positivo, la siguiente sube el 49 % de las veces: no predice." in r["pred"]
    assert all("cripto" in x["sub"] or x.get("val") is None for x in r["info"])
    assert r["r0"] == "pendiente" and r["r1"]["estado"] == "confirmada" and r["sent"] == 0
    assert r["r1"]["umbral_pct"] >= 0.5 and r["r1"]["mov_pct"] > r["r1"]["umbral_pct"]
    _sin_indices(r["mk"], r["pred"], r["md"], solo_nombres=True)
    _sin_null(r["mk"], r["pred"], r["md"], r["right"])


def test_reflejo_con_la_clase_de_la_captada_umbral_y_direccion_del_triaje():
    """Revisión del 28-10-2026: (10) una macro (patch.clase de la captada) con dirección no cuenta hasta que la cripto la
    confirme, aunque el «reflejo» aún no haya llegado, y una de cripto cuenta siempre; (11) en una macro confirmada manda
    la dirección del triaje, no la del impacto; (6) el expediente enseña el umbral y lo que saldría por azar; (9) el aviso
    de velas incompletas se ve escapado; (8) el solape es de 30 a 90 min."""
    r = run(CRIPTO_AP + """H.open(); reset();
      const tr = dir => ({tipo: "macro", importancia: 7, credibilidad: 80, sentimiento: dir > 0 ? "alcista" : "bajista",
        repercusion: {direccion: 60*dir, magnitud: 70, activos: []}, monedas: []});
      H.recv(ev("captured", "m5", {headline: "EE. UU. impone aranceles", src: "AP", tier: "T1", t0: now, coins: [], clase: "macro"}),
             ev("triaged", "m5", {triage: tr(-1)}));
      H.recv(ev("captured", "n5", {headline: "Reunión rutinaria", src: "AP", tier: "T1", t0: now, coins: [], clase: "macro"}),
             ev("triaged", "n5", {triage: {...tr(1), repercusion: {direccion: 10, magnitud: 70, activos: []}}}));
      H.recv(ev("captured", "c5", {headline: "Hackeo en Bithumb", src: "CoinDesk", tier: "T2", t0: now, coins: [], clase: "cripto"}),
             ev("triaged", "c5", {triage: {...tr(-1), tipo: "seguridad"}}),
             ev("reflejo", "c5", {reflejo: {estado: "sin_reflejo", mov_pct: 0.1, min: 60, ref: "media"}}));
      H.recv(ev("captured", "m6", {headline: "Irán cierra Ormuz", src: "Reuters", tier: "T1", t0: now, coins: [], clase: "macro"}),
             ev("triaged", "m6", {triage: tr(-1)}),
             ev("impact", "m6", {impact: {dir: 1, mag: 2, conf: 0.8, per: {BTC: 2}, main: "BTC", hz: "horas"}}),
             ev("reflejo", "m6", {reflejo: {estado: "confirmada", mov_pct: -1.2, min: 9, ref: "media", umbral_pct: 0.93}}));
      const t = Date.now(), nb = newsBalance(t), ids = a => a.map(x => x.st.id).sort();
      const tilt = newsTilt(S.stories.get("m6"), t), ok = ["m5", "n5", "c5", "m6"].map(id => reflejoOk(S.stories.get(id)));
      H.fire("click", {"data-tab": "world"}); World.panes(); const right = H.el("#wRight").innerHTML;
      S.tab = "live"; S.sel = "m6"; renderTrace(); const tr6 = H.el("#trace").innerHTML;
      H.recv(ca({londres: {name: "Londres", window: "europa", state: "terminada", open_ms: now - 5*HORA, next_open_ms: now + 19*HORA,
        minutes_since_open: 300, coins: null, last: null, hist: null, aviso: "Faltan velas de <b>BTC</b> para completar las 4 h"}},
        {chain: {after_up_rate: 0.49, after_down_rate: 0.51, base_rate: 0.5, n: 100, nota: "sin mencionarlo"}}));
      renderWorldMarkets(); const mk = H.el("#mkTiles").innerHTML; const chain = caChain();
      return {items: ids(nb.items), wait: ids(nb.waiting), tilt, ok, right, tr6, mk, chain};""")
    assert r["ok"] == [False, True, True, True]
    assert r["items"] == ["c5", "m6", "n5"] and r["wait"] == ["m5"]
    assert r["tilt"]["dir"] == -1 and r["tilt"]["why"] == "triaje local"   # el impacto alcista no vale en una macro bajista
    assert "Esperando al mercado" in r["right"]
    assert "Umbral" in r["tr6"] and "0,93 %" in r["tr6"] and "Por puro azar" in r["tr6"] and "5 %" in r["tr6"]
    assert "Puede ser casualidad" in r["tr6"] and "2σ" in r["tr6"]
    assert "Faltan velas de &lt;b&gt;BTC&lt;/b&gt;" in r["mk"] and "<b>BTC</b> para" not in r["mk"]
    assert "entre 30 y 90 min" in r["chain"] and "30 min de diferencia" not in r["chain"]
    _sin_null(r["right"], r["tr6"], r["mk"], r["chain"])


def test_demostracion_terminada_solo_si_hoy_hay_sesion():
    """Hallazgo n.º 15: en la demostración, Tokio el sábado a la 01:00 (hora de Tokio) ya no sale «terminada» con la
    sesión del viernes (el bot dice «por_abrir»); el viernes a las 15:00, sí."""
    r = run("""const sab = Date.UTC(2026, 9, 30, 16, 0), vie = Date.UTC(2026, 9, 30, 6, 0);
      const a = simCriptoAp(sab).markets.tokio, b = simCriptoAp(vie).markets.tokio;
      return {sab: a.state, sabLast: a.last === null || typeof a.last === "object", vie: b.state, um: simUmbral(S.vnow)};""",
            live=False)
    assert r["sab"] == "por_abrir" and r["sabLast"] and r["vie"] == "terminada" and r["um"] >= 0.5


# Noticias fiables (bot/fiables.py, mensaje «noticias_fiables» y GET /api/noticias_fiables), entrada por noticia fiable
# (bot/entrada_noticias.py, evento «entrada_noticia») y preset «seis» del motor de aperturas («6 diarias»), 28-10-2026.
FIAB = r"""
const DIA = d => `2026-10-${d}`;
const grp = (o = {}) => ({clave: "medio:rbc.ru", tipo: "medio", nombre: "rbc.ru", sentido: null, n: 36, dias: 3, dias_validos: 3,
  acierto: 0.722, acierto_bruto: 0.5, acierto_1h: 0.472, n_1h: 36, ic90_bajo: 0.587, outlet: "rbc.ru",
  por_dia: [{dia: DIA(25), n: 8, acierto: 0.875}, {dia: DIA(26), n: 20, acierto: 0.75}, {dia: DIA(27), n: 8, acierto: 0.5}], estado: "fiable", ...o});
const fiab = (o = {}) => ({type: "noticias_fiables", ts: now, desde: DIA(25), hasta: DIA(27), dias: 3, dias_lista: [DIA(25), DIA(26), DIA(27)], unidades: 6597,
  criterios: {n_min: 30, dias_min: 3, n_dia: 5, acierto_min: 0.6, wilson90_min: 0.5, dia_min: 0.5, horizonte_min: 240, permutaciones: 500},
  probados: 60, con_muestra: 22, pasan: 1, por_azar: 3.47, por_azar_p95: 6, por_azar_max: 9,
  familias: {medio: {probados: 19, pasan: 1, por_azar: 1.0}, fuente: {probados: 12, pasan: 0, por_azar: 0.19}},
  grupos: [grp({nombre: "rbc.ru <b>"}),
           grp({clave: "fuente:x", tipo: "fuente", nombre: "Canal X", n: 41, acierto: 0.61, ic90_bajo: 0.48, estado: "no_fiable",
                por_dia: [{dia: DIA(25), n: 15, acierto: 0.733}, {dia: DIA(26), n: 10, acierto: 0.4}, {dia: DIA(27), n: 3, acierto: 0.667}]}),
           grp({clave: "tipo_sentido:energía:-1", tipo: "tipo_sentido", nombre: "energía · bajista", sentido: -1, n: 12, dias: 2, dias_validos: 2,
                acierto: 0.75, ic90_bajo: 0.51, estado: "en_prueba", por_dia: [{dia: DIA(26), n: 6, acierto: 0.833}, {dia: DIA(27), n: 6, acierto: 0.667}]})],
  nota: "1 de 60 grupos pasan <i>; con la dirección barajada pasarían 3,5 de media.", ...o});
const ent = (story, coin, estado, motivo, o = {}) => ev("entrada_noticia", story, {entrada_noticia: {estado, motivo, coin, dir: 1,
  grupo: {clave: "medio:rbc.ru", nombre: "rbc.ru <b>", tipo: "medio", acierto: 0.722, n: 36}, umbral_pct: 0.62, mov_pct: 0.71,
  hasta: now + 3600e3, ...o}}, {coins: [coin], title: `Compra ${coin} · noticia fiable`, text: motivo});
const SEIS_P = {estrategia: "impulso", measure_min: 15, min_move_pct: 1e-9, entry: "t:15", rvol_min: null, vol_filter: "todos", btc_filter: "ninguno",
  news_mode: "fiables", windows: ["tokio-seul", "china", "europa", "saopaulo", "nuevayork"], coins: ["BTC", "ETH", "SOL", "XRP"], sl_pct: 1.5,
  target_r: 3, max_hold_h: 24, min_net_pct: 3, avg_hold_h: 13, long_instrument: "spot", short_instrument: "perp", risk_pct: null,
  max_day_entries: 6, max_day_risk_pct: 3, lab: null};
const opn = (params, o = {}) => ({type: "openings", ts: now, strategy: "impulso", params, params_source: "evolucion", notice: null, windows: [], ...o});
"""


def test_noticias_fiables_panel_criterios_azar_y_estados():
    """Pestaña Evolución: criterios en lenguaje llano, probados / pasan / «por puro azar pasarían», tabla con los 3
    estados (fiables primero), minibarra por día y filtro; resumen corto en Mando. Todo escapado y sin null."""
    r = run(FIAB + """H.open(); reset(); H.recv(fiab()); S.tab = "evo"; evoBump(); renderAll(true);
      const a = {head: H.el("#fiabHead").innerHTML, tbl: H.el("#fiabTbl").innerHTML, f: H.el("#fiabF").innerHTML};
      H.fire("click", {"data-fiab-f": "fiable"}); renderAll(true);
      const solo = H.el("#fiabTbl").innerHTML, f2 = H.el("#fiabF").innerHTML;
      S.tab = "mando"; renderAll(true);
      return {...a, solo, f2, md: H.el("#mdFiab").innerHTML, sub: H.el("#mdFiabSub").textContent};""")
    head, tbl = r["head"], r["tbl"]
    for t in ("≥ 60 %", "al menos 30 historias", "al menos 3 días con ≥ 5 historias", "Ajustado al mercado",
              "también las que llegan tarde", "constancia", "Wilson", "(500 veces)", "nada mira el futuro", "acierta ≥ 50 % en cada uno"):
        assert t in head, t
    assert "<small>Probados</small><b>60</b>" in head and '<small>Pasan</small><b><span class="pos">1</span></b>' in head
    assert "por puro azar pasarían ~3,5" in head and "hasta 6 el 95 % de las veces" in head and "Medios: 1 de 19 (azar ~1)" in head
    assert "1 de 60 grupos pasan &lt;i&gt;" in head and "pasan <i>" not in head
    assert "con el sentido acertado el 50 % de las veces se pierde" in head
    assert "rbc.ru &lt;b&gt;" in tbl and "rbc.ru <b>" not in tbl
    i_f, i_p, i_n = tbl.index(">Fiable</span>"), tbl.index(">En prueba</span>"), tbl.index(">No fiable</span>")
    assert i_f < i_p < i_n   # fiables primero y luego por acierto
    assert tbl.count("<svg") == 3 and "26/10: 10 historias×moneda, acierto 40 %" in tbl and "27/10: 3 historias" in tbl
    assert "no cuenta para la constancia" in tbl and "72 %" in tbl and "59 %" in tbl and "Tipo y sentido" in tbl
    assert 'data-fiab-f="fiable" aria-pressed="false">Fiables · 1' in r["f"] and "Tipo y sentido · 1" in r["f"]
    assert 'data-fiab-f="fiable" aria-pressed="true"' in r["f2"] and "rbc.ru" in r["solo"] and "Canal X" not in r["solo"]
    assert "rbc.ru &lt;b&gt;" in r["md"] and "acierto 72 %" in r["md"] and 'data-tabgo="evo"' in r["md"]
    assert r["sub"].startswith("Pasan 1 de 60 grupos · por puro azar pasarían ~3,5")
    _sin_null(head, tbl, r["f"], r["solo"], r["md"], r["sub"])


def test_noticias_fiables_por_la_api_sin_grupos_y_404():
    """Al conectar se pide GET /api/noticias_fiables; sin grupos fiables sale la nota honesta; el bus (más nuevo) manda
    y uno más viejo no lo pisa; un bot sin la ruta (404) no rompe nada y se espera al mensaje."""
    r = run(FIAB + """H.fetchReply = url => url === "/api/noticias_fiables" ? {ok: true, json: async () => fiab({ts: now - 5000, pasan: 0, nota: null,
        grupos: [grp({estado: "no_fiable", acierto: 0.52})]})} : {ok: false};
      H.open(); reset(); await loadApi();
      S.tab = "evo"; evoBump(); renderAll(true); const cero = H.el("#fiabHead").innerHTML;
      S.tab = "mando"; renderAll(true); const md0 = H.el("#mdFiab").innerHTML;
      H.recv(fiab()); const nuevo = S.fiables.pasan;
      H.recv(fiab({ts: now - 60000, pasan: 7})); const sigue = S.fiables.pasan;
      const nf0 = H.fetches.filter(u => u === "/api/noticias_fiables").length;
      H.fetchReply = null; reset(); await loadApi(); S.tab = "evo"; evoBump(); renderAll(true); const vacio = H.el("#fiabHead").innerHTML;
      S.tab = "mando"; renderAll(true);
      return {cero, md0, nuevo, sigue, nf0, vacio, md: H.el("#mdFiab").innerHTML, sub: H.el("#mdFiabSub").textContent, none: S.fiables === null};""")
    assert "no hay medios que acierten ≥ 60 % de forma constante; el bot sigue midiendo" in r["cero"]
    assert '<small>Pasan</small><b><span class="">0</span></b>' in r["cero"] and "por puro azar pasarían ~3,5" in r["cero"]
    assert "todavía no hay medios que acierten ≥ 60 % de forma constante" in r["md0"] and "sin grupos fiables no se abre ninguna" in r["md0"]
    assert r["nuevo"] == 1 and r["sigue"] == 1 and r["nf0"] >= 1
    assert r["none"] and "aún no ha llegado" in r["vacio"] and "≥ 60 %" in r["vacio"] and "Sin cálculo" in r["md"]
    assert r["sub"].startswith("Aún no ha llegado el cálculo")
    _sin_null(r["cero"], r["md0"], r["vacio"], r["md"])


def test_noticias_fiables_toleran_campos_ausentes_o_rotos():
    r = run(FIAB + """H.open(); reset(); S.tab = "evo"; renderAll(true); const v = H.el("#fiabHead").innerHTML;
      H.recv({type: "noticias_fiables"}); evoBump(); renderAll(true); const a = H.el("#fiabHead").innerHTML + H.el("#fiabTbl").innerHTML;
      H.recv({type: "noticias_fiables", ts: now + 1, criterios: "x", probados: "x", pasan: null, por_azar: {media: 2.5, p95: 5}, dias: ["2026-10-25"],
              desde: 5, hasta: "x", familias: {medio: null, fuente: {pasan: "x"}}, nota: 5,
              grupos: [null, 7, {estado: "rara", tipo: 9, nombre: null, acierto: 72.6, ic90_bajo: "x", sentido: "x",
                       por_dia: [null, {dia: null, n: "x"}, {dia: 1793000000000, n: 6, acierto: 55}]}, {}]});
      evoBump(); renderAll(true); const b = H.el("#fiabHead").innerHTML + H.el("#fiabTbl").innerHTML + H.el("#fiabF").innerHTML;
      S.tab = "mando"; renderAll(true);
      return {v, a, b, md: H.el("#mdFiab").innerHTML + H.el("#mdFiabSub").textContent};""")
    assert "aún no ha llegado" in r["v"] and "73 %" in r["b"] and "~2,5" in r["b"] and "rara" in r["b"] and "sin estado" in r["b"]
    _sin_null(r["v"], r["a"], r["b"], r["md"])


def test_entrada_por_noticia_cuatro_estados_en_cable_mando_y_expediente():
    """Eventos «entrada_noticia»: esperando, confirmada, caducada y vetada (con motivo) en el cable, en Mando y en el
    expediente; sin verificación ni impacto por medio, y un estado desconocido no rompe nada."""
    r = run(FIAB + """H.open(); reset(); H.recv(fiab());
      H.recv(ev("captured", "n1", {headline: "Titular <i>", src: "rbc.ru", tier: "T2", t0: now - 60e3, coins: ["BTC"]}),
             ev("triaged", "n1", {triage: {tipo: "empresas", importancia: 4, temas: [], monedas: ["BTC"], repercusion: {direccion: 50}}}),
             ent("n1", "BTC", "esperando", "Espera hasta 60 min a que BTC se mueva 0,62 % a su favor"),
             ent("n2", "ETH", "confirmada", "ETH se movió +0,71 %"), ent("n3", "SOL", "caducada", "En 60 min no se movió"),
             ent("n4", "XRP", "vetada", "Tope de 6 entradas del día <b>"),
             ev("entrada_noticia", "n5", {}, {coins: ["BTC"], estado: "rara"}));
      S.sel = "n1"; renderTrace(); const tr1 = H.el("#trace").innerHTML;
      S.sel = "n4"; renderTrace(); const tr4 = H.el("#trace").innerHTML;
      S.tab = "live"; lastFeedSig = ""; renderAll(true); const feed = H.el("#feed").innerHTML;
      S.tab = "mando"; renderAll(true);
      return {tr1, tr4, feed, md: H.el("#mdFiab").innerHTML, mdFeed: H.el("#mdFeed").innerHTML,
              tones: S.events.filter(e => e.kind === "entrada_noticia").map(e => e.tone)};""")
    feed, md, tr1, tr4 = r["feed"], r["md"], r["tr1"], r["tr4"]
    for t in ("Noticia fiable · Esperando confirmación", "Noticia fiable · Confirmada → operación", "Noticia fiable · Caducada",
              "Noticia fiable · Vetada", "Noticia fiable · Entrada por noticia"):
        assert t in feed, t
    assert "Tope de 6 entradas del día &lt;b&gt;" in feed and "día <b>" not in feed and "Noticia fiable · Vetada" in r["mdFeed"]
    assert r["tones"] == ["warn", "good", "muted", "bad", "muted"]
    for led, txt in (("wait", "Esperando confirmación"), ("busy", "Confirmada → operación"), ("off", "Caducada"), ("stop", "Vetada")):
        assert f'<span class="led {led}"></span>' in md and txt in md, txt
    assert 'data-story="n4"' in md and "XRP ▲" in md and "Tope de 6 entradas del día &lt;b&gt;" in md
    assert "Entrada por noticia fiable" in tr1 and "Noticia fiable · Esperando confirmación" in tr1 and "rbc.ru &lt;b&gt;" in tr1
    assert "0,62 % a favor" in tr1 and "72 % en 36 historias" in tr1 and "No hace falta para la entrada por noticia fiable" in tr1
    assert "stop 1,5 % y objetivo 3R, como mucho 24 h" in tr1 and "Titular &lt;i&gt;" in tr1
    assert "Vetada" in tr4 and 'class="reason">Tope de 6 entradas del día &lt;b&gt;' in tr4
    _sin_null(feed, md, tr1, tr4, r["mdFeed"])


def test_operaciones_por_noticia_fiable_y_estrategia_activa():
    """Resultados y Cartera marcan las operaciones con origen «noticia_fiable» (enlazan a su noticia) y enseñan el nombre
    de la estrategia activa del motor (params.nombre); con «6 diarias», también el aviso de que es tu decisión."""
    r = run(FIAB + """H.open(); reset();
      const tr = (o = {}) => ({id: "t1", story: "n9", strategy: "noticia_fiable-largo", window: "Noticia", market: "", coin: "SOL", side: "long",
        qty: 1, entry: 100, exit: 104.5, opened: now - 3600e3, closed: now, reason: "Objetivo", pnl: 1.5, pnl_pct: 4.5, R: 3, fees: 0.1,
        headline: "Titular <b>", instrument: "spot", funding: 0, commissions: 0.1,
        tags: {origen: "noticia_fiable", grupo: {clave: "medio:rbc.ru", nombre: "rbc.ru <i>"}}, ...o});
      S.results = {start_equity: 111.37, equity: 112.87, trades: [tr(), tr({id: "t2", story: "n8", strategy: "impulso-largo-sin-noticia", tags: {origen: "motor_aperturas"}})],
        by_strategy: [], by_window: [], by_coin: [], equity_curve: [[now - 7200e3, 111.37], [now, 112.87]], daily: []};
      S.tab = "results"; renderAll(true); const sin = H.el("#resStrat").innerHTML;
      H.recv(opn({...SEIS_P, preset: "seis", nombre: "6 diarias"}), pf({closed: [{...tr(), closedAt: now}]}),
             ev("strategy", "n7", {headline: "h7", src: "s", tier: "T1", t0: now, strat: strat("ETH", "long", {stopPct: 1.5, tp1R: 3, tp2R: 3, tp1Frac: 0,
               trailPct: null, origen: "noticia_fiable", tags: {origen: "noticia_fiable"}})}));
      S.btImp = {seis: {fuera: {n: 1538, mean_R: -0.12}}};
      renderAll(true);
      const res = {k: H.el("#resStrat").innerHTML, h: H.el("#resStrat").hidden, rows: H.el("#resRows").innerHTML, news: H.el("#resByNews").innerHTML};
      S.tab = "trades"; renderAll(true);
      const trd = {s: H.el("#tradesStrat").innerHTML, rows: H.el("#closedRows").innerHTML, kb: H.el("#kanban").innerHTML};
      S.tab = "market"; renderAll(true); const mk = H.el("#openings").innerHTML, sub = H.el("#openSub").textContent;
      H.recv(opn({preset: "impulso", nombre: "Impulso E2 <b>"})); S.tab = "results"; renderAll(true);
      return {sin, res, trd, mk, sub, imp: H.el("#resStrat").innerHTML};""")
    res, trd = r["res"], r["trd"]
    assert "aún no ha publicado su estado" in r["sin"]
    assert res["h"] is False and "6 diarias" in res["k"] and "preset 6 diarias" in res["k"] and "Puesta por tu decisión" in res["k"]
    assert "-0,12 R por operación fuera de muestra" in res["k"] and "1 operación por noticia fiable" in res["k"]
    assert res["rows"].count(">Noticia fiable</span>") == 1 and 'data-story="n9"' in res["rows"] and "grupo fiable rbc.ru &lt;i&gt;" in res["rows"]
    assert "Titular &lt;b&gt;" in res["rows"] and "Por noticia fiable (entrada propia)" in res["news"]
    assert "6 diarias" in trd["s"] and ">Noticia fiable</span>" in trd["rows"] and ">Noticia fiable</span>" in trd["kb"]
    assert r["sub"].startswith("Modo impulso «6 diarias» (paper, por tu decisión)")
    for t in ("(cualquier movimiento)", "entrada a +15 min", "sin filtro de volumen", "sin filtro de BTC",
              "veta la entrada si hay una noticia fiable en contra", "stop 1,50 % y objetivo 3,0 R (+4,50 %)", "como mucho 24 h"):
        assert t in r["mk"], t
    assert "Impulso E2 &lt;b&gt;" in r["imp"] and "preset impulso" in r["imp"] and "por tu decisión" not in r["imp"]
    _sin_null(r["sin"], res["k"], res["rows"], res["news"], trd["s"], trd["rows"], trd["kb"], r["mk"], r["imp"])


SEIS_BT = {"generated": 1793188024481, "split": 1755295200000, "sl_pct": 1.5, "nota": "Réplica del motor <b>", "nombre": "6 diarias",
           "params": {"measure_min": 15, "min_move_pct": 1e-09, "entry": "t:15", "rvol_min": None, "vol_filter": "todos", "btc_filter": "ninguno",
                      "news_mode": "fiables", "sl_pct": 1.5, "target_r": 3.0, "max_hold_h": 24.0, "min_net_pct": 3.0, "max_day_entries": 6},
           "fuera": {"n": 1538, "mean_R": -0.12, "win_rate": 0.3524, "salidas": {"stop": 865, "tiempo": 482, "objetivo": 191}, "horas_media": 13.1,
                     "largos": {"n": 685, "mean_R": -0.2154}, "cortos": {"n": 853, "mean_R": -0.0434}, "desde": 1755476100000, "hasta": 1792505400000,
                     "curva_R": [[1755480600000, -1.145], [1792505400000, -184.6]], "paper_R": -0.12, "paper_R_funding": -0.119},
           "dentro": {"n": 2536, "mean_R": -0.1112, "win_rate": 0.3289, "desde": 1698624900000, "hasta": 1755328500000,
                      "curva_R": [[1698646140000, -1.145], [1755328500000, -282.0]]}}
IMP_BT = {"generated": 1793150000000, "split": 1755295200000, "nota": "E2",
          "fuera": {"n": 202, "mean_R": 0.3459, "win_rate": 0.41, "salidas": {"stop": 113, "objetivo": 60, "tiempo": 29}, "horas_media": 64.9,
                    "largos": {"n": 85, "mean_R": 0.098}, "cortos": {"n": 117, "mean_R": 0.526}, "desde": 1755655200000, "hasta": 1790778780000,
                    "curva_R": [[1755655200000, 0.5], [1790778780000, 69.88]]},
          "dentro": {"n": 469, "mean_R": 0.1297, "win_rate": 0.33, "desde": 1698845400000, "curva_R": [[1698845400000, -1], [1755481500000, 60.8]]}}


def test_backtest_estrategia_en_marcha_seis_o_impulso():
    """Con p.preset «seis», la pestaña Backtest abre con «Estrategia en marcha: 6 diarias» (KPI, curva y avisos: fuera de
    muestra pierde y está en la cartera por tu decisión) y E2 pasa a «Estrategia anterior». Con «impulso» o sin dato,
    E2 sigue en marcha y «6 diarias» sale debajo como medida y no en marcha."""
    page = HTML.read_text(encoding="utf-8")
    r = run("const now = Date.now();" + FIAB + f"""S.btImp = {{impulso: {json.dumps(IMP_BT)}, lab: null, seis: {json.dumps(SEIS_BT)}}};
      const out = {{}};
      for (const [k, m] of [["seis", opn({{...SEIS_P, preset: "seis", nombre: "6 diarias"}})], ["impulso", opn({{preset: "impulso"}})], ["nada", null]]){{
        S.openings = m; S.tab = "bt"; S.btSig = null; renderAll(true);
        out[k] = {{title: H.el("#btLiveTitle").textContent, sub: H.el("#btLiveSub").textContent, top: H.el("#btSeisTopP").hidden, alt: H.el("#btSeisAltP").hidden,
          seis: (k === "seis" ? H.el("#btSeisTop") : H.el("#btSeisAlt")).innerHTML + H.el("#btSeisKpis").innerHTML,
          e2: H.el("#btLive").innerHTML + H.el("#btLiveKpis").innerHTML}}; }}
      S.btImp = {{impulso: null, lab: null, seis: null}}; S.openings = opn({{preset: "seis"}}); S.btSig = null; renderAll(true);
      out.vacio = {{top: H.el("#btSeisTopP").hidden, html: H.el("#btSeisTop").innerHTML, title: H.el("#btLiveTitle").textContent}};
      return out;""", live=False)
    s, i, n = r["seis"], r["impulso"], r["nada"]
    assert s["title"] == "Estrategia anterior (E2): impulso de los primeros minutos" and s["sub"].startswith("Ya no opera")
    assert s["top"] is False and s["alt"] is True and "+0,35 R" in s["e2"]
    h = s["seis"]
    for t in ("Fuera de muestra pierde", "-0,12 R por operación en 1538 operaciones", "por jornada de lunes a viernes", "por día natural",
              "Está en tu cartera paper por tu decisión, para medirla en vivo", "de −0,12 a −0,17 R por operación", "de −0,32 a −0,44 %",
              "Con 3R el objetivo se cobra en 12 % de las operaciones", "13-17 %", "no lleva el veto de noticias fiables",
              "desde el 28-10-2026", "Réplica del motor &lt;b&gt;", "veta la entrada si hay una noticia fiable en contra",
              "<small>Fuera de muestra</small>", "<small>Por jornada</small>", "con funding -0,12 R", "-184,60 R", "sin interés compuesto"):
        assert t in h, t
    assert "-0,30 %" in h and "-0,22 %" in h   # 5,0 operaciones por jornada y 3,6 por día natural con 0,5 % de riesgo
    assert i["title"] == "Estrategia en marcha: impulso de los primeros minutos (E2)" and i["sub"].startswith("Lo que opera ahora")
    assert i["top"] is True and i["alt"] is False and "Si la activas, será por tu decisión" in i["seis"] and "-0,12 R" in i["seis"]
    # sin «openings» (la copia publicada, o antes del primer mensaje) no se sabe qué opera: ni E2 ni «6 diarias» «en marcha»
    assert n["title"] == "Estrategia E2 (impulso de los primeros minutos): backtest de referencia"
    assert n["sub"].startswith("Todavía no se sabe qué estrategia opera el motor") and "en marcha" not in n["title"]
    assert n["top"] is True and n["alt"] is False and "La activa o la quita solo tu decisión" in n["seis"]
    assert "Si la activas" not in n["seis"] and "-0,12 R" in n["seis"]
    assert r["vacio"]["top"] is False and "no trae el backtest de «6 diarias»" in r["vacio"]["html"]
    assert r["vacio"]["title"].startswith("Estrategia anterior (E2)")
    _sin_null(h, s["e2"], i["seis"], n["seis"], r["vacio"]["html"])
    if 'id="seisData"' in page:   # el resumen real incrustado (scripts/incrustar_backtest.py) también se pinta sin huecos
        blob = page.split('<script type="application/json" id="seisData">', 1)[1].split("</script>", 1)[0]
        r2 = run("const now = Date.now();" + FIAB + f"""S.btImp = {{impulso: null, lab: null, seis: {blob}}}; S.openings = opn({{preset: "seis"}}); S.tab = "bt"; S.btSig = null; renderAll(true);
          return H.el("#btSeisTop").innerHTML + H.el("#btSeisKpis").innerHTML;""", live=False)
        assert "Fuera de muestra" in r2 and "por tu decisión" in r2
        _sin_null(r2)


def test_demostracion_noticias_fiables_y_entradas_por_noticia():
    """Sin bot: noticias fiables de EJEMPLO rotuladas (0-2 fiables y «por azar» parecido) y entradas por noticia con
    los cuatro estados; la confirmada entra por el camino de siempre con horquilla pura (sin break-even, 3R, 24 h)."""
    r = run("""S.tab = "evo"; renderAll(true); const head = H.el("#fiabHead").innerHTML, tbl = H.el("#fiabTbl").innerHTML;
      S.tab = "mando"; renderAll(true); const md = H.el("#mdFiab").innerHTML, sub = H.el("#mdFiabSub").textContent;
      const F = S.fiables, fi = F.grupos.filter(g => g.estado === "fiable").length, hist = S.events.filter(e => e.kind === "entrada_noticia").length;
      S.positions.length = 0; S.scheduled.length = 0; S.closed.length = 0; S.pending.length = 0; S.queue.length = 0; S.execStop = false;
      const nueva = (id, c) => emit({kind: "captured", story: id, title: "Prueba " + id, coins: [c], patch: {headline: "Prueba " + id, src: "x", tier: "T1", t0: S.vnow, coins: [c]}});
      const g = demoFiablePara("regulatorio", 1), est = id => S.stories.get(id).events.filter(e => e.kind === "entrada_noticia").map(e => entInfo(e).estado);
      nueva("pc", "SOL"); simEntrada("pc", 1, g); S.prices.SOL *= 1.05; step(61000);
      const tk = S.stories.get("pc").strat; sendOrder("pc"); step(2000);
      const pos = S.positions.find(p => p.id === "pc"), br = pos ? {bracket: pos.bracket, tp1: pos.tp1, tags: pos.tags.origen} : null;
      if (pos){ S.prices.SOL = pos.tp2 * 1.001; managePositions(); }
      const cerrada = S.closed.find(x => x.id === "pc");
      S.positions.push({id: "otra", coin: "XRP", side: "long", qty: 1, entry: 1, sl: 0.9, tp1: null, tp2: 2, notional: 1, riskUsd: 0.1, opened: S.vnow, be: false, bracket: true, maxAge: 864e5});
      nueva("pv", "XRP"); simEntrada("pv", 1, g); S.prices.XRP *= 1.05; step(61000);
      nueva("pk", "ETH"); const p0 = S.prices.ETH; simEntrada("pk", 1, g); for (let i = 0; i < 62; i++){ S.prices.ETH = p0; step(61000); }
      S.tab = "trades"; renderAll(true);
      return {head, tbl, md, sub, fi, hist, pasan: F.pasan, azar: F.por_azar, demo: F.demo, pc: est("pc"), pv: est("pv"), pk: est("pk"),
              tk: {bracket: tk.bracket, stop: tk.stopPct, r: tk.tp2R, origen: tk.tags.origen, maxAge: tk.maxAgeMs}, br,
              cerrada: cerrada ? cerrada.why : null, rows: H.el("#closedRows").innerHTML, sockets: H.sockets.length};""", live=False)
    assert r["demo"] is True and 0 <= r["fi"] <= 2 and r["pasan"] == r["fi"] and abs(r["pasan"] - r["azar"]) <= 2
    assert "Ejemplo inventado" in r["head"] and "por puro azar pasarían ~1,6" in r["head"] and ">Fiable</span>" in r["tbl"]
    assert "ejemplo inventado" in r["sub"] and "regulatorio · alcista" in r["md"]
    assert r["pc"] == ["esperando", "confirmada"] and r["pv"] == ["esperando", "vetada"] and r["pk"] == ["esperando", "caducada"]
    assert r["tk"] == {"bracket": True, "stop": 1.5, "r": 3, "origen": "noticia_fiable", "maxAge": 86400000}
    assert r["br"] == {"bracket": True, "tp1": None, "tags": "noticia_fiable"} and r["cerrada"] == "Objetivo (3 R)"
    assert ">Noticia fiable</span>" in r["rows"] and r["sockets"] == 0
    _sin_null(r["head"], r["tbl"], r["md"], r["sub"], r["rows"])


def test_fiables_puerta_para_operar_azar_por_horas_y_noticias_distintas():
    """Revisión del 28-10-2026: cada grupo enseña sus noticias distintas además de las historias×moneda, si opera o no
    (con lo que le falta), la cota por bloques de 30 min y el azar que conserva la hora con su valor p."""
    r = run(FIAB + """H.open(); reset();
      H.recv(fiab({operan: 0, p_azar: 0.11, azar_bloques: {media: 3.8, p95: 7, max: 10, p: 0.186, bloque_min: 60},
        puerta: {texto: "Para operar hace falta <b>más</b>."},
        grupos: [grp({n_historias: 9, opera: false, bloques: 7, ic90_bajo_bloques: 0.41, vivo: {n: 32, n_historias: 8, acierto: 0.78},
          adelante: {desde: DIA(26), n: 10, n_historias: 3, acierto: 0.4, acierto_bruto: 0.3}, no_opera: ["9 noticias distintas (hacen falta 30)", "acierto bruto 34 % <i>"],
          por_dia: [{dia: DIA(25), n: 8, n_historias: 2, acierto: 0.875}]})]}));
      S.tab = "evo"; evoBump(); renderAll(true);
      const head = H.el("#fiabHead").innerHTML, tbl = H.el("#fiabTbl").innerHTML;
      S.tab = "mando"; renderAll(true);
      return {head, tbl, md: H.el("#mdFiab").innerHTML, sub: H.el("#mdFiabSub").textContent};""")
    head, tbl, md = r["head"], r["tbl"], r["md"]
    assert "conservando la hora ~3,8 (p = 0,186)" in head and "con el azar que conserva la hora (p = 0,186)" in head
    assert "Pasan la puerta para operar: 0." in head and "· 0 operan" in head and "Para operar hace falta &lt;b&gt;más&lt;/b&gt;." in head
    assert "(22 con la muestra mínima)" in head and "historias×moneda" in head and "noticias distintas" in head
    assert "a 4 h desde el triaje y ajustado al mercado) y no garantiza ese 60 %" in head
    assert ">No opera</span>" in tbl and "No opera: 9 noticias distintas (hacen falta 30); acierto bruto 34 % &lt;i&gt;" in tbl
    assert "9 noticias</span>" in tbl and "por bloques 41 %" in tbl and "8 historias×moneda (2 noticias)" in tbl
    assert "en vivo 78 % (8)" in tbl and "hacia delante 40 % (3)" in tbl and "(desde el 26/10)" in tbl
    assert "9 noticias · 3 días · no opera" in md and r["sub"].endswith("· operan 0")
    assert "ningún grupo fiable pasa la puerta para operar: no se abre ninguna" in md
    _sin_null(head, tbl, md, r["sub"])


def test_entrada_con_la_forma_del_backend_y_operacion_enlaza_la_noticia():
    """El evento tal como lo publica bot/entrada_noticias.py (moneda, expira y el grupo corto con noticias distintas)
    enseña «Espera hasta»; la operación por noticia (story nf-…) abre la noticia que la disparó (tags.story)."""
    r = run(FIAB + """H.open(); reset();
      H.recv(ev("captured", "n1", {headline: "Titular", src: "rbc.ru", tier: "T2", t0: now - 60e3, coins: ["BTC"]}),
             ev("entrada_noticia", "n1", {entrada_noticia: {estado: "esperando", motivo: "Espero", moneda: "BTC", dir: 1, lado: "largo",
               grupo: {clave: "medio:rbc.ru", tipo: "medio", nombre: "rbc.ru", sentido: null, n: 36, n_historias: 9, dias: 3, acierto: 0.722,
                       acierto_bruto: 0.5, ic90_bajo: 0.587, opera: true}, ta: now, expira: Date.UTC(2026, 9, 28, 11, 1), ts_triaje: now}},
               {coins: ["BTC"], title: "Noticia fiable: espero confirmación del precio en BTC", text: "Espero"}));
      S.sel = "n1"; renderTrace(); const trace = H.el("#trace").innerHTML;
      const tr = {id: "t1", story: "nf-n1-btc", strategy: "noticia-fiable-largo", window: "Noticia fiable", market: "", coin: "BTC", side: "long",
        qty: 1, entry: 100, exit: 98.5, opened: now - 3600e3, closed: now, reason: "Stop", pnl: -0.5, pnl_pct: -1.5, R: -1, fees: 0.1,
        headline: "h", instrument: "spot", funding: 0, commissions: 0.1, tags: {origen: "noticia_fiable", story: "n1", grupo: "medio:rbc.ru"}};
      S.results = {start_equity: 111.37, equity: 110.87, trades: [tr], by_strategy: [], by_window: [], by_coin: [],
                   equity_curve: [[now - 7200e3, 111.37], [now, 110.87]], daily: []};
      H.recv(pf({closed: [{...tr, closedAt: now}]}));
      S.tab = "results"; renderAll(true); const res = H.el("#resRows").innerHTML;
      S.tab = "trades"; renderAll(true);
      return {trace, res, cl: H.el("#closedRows").innerHTML};""")
    assert "Espera hasta" in r["trace"] and "12:01 Madrid" in r["trace"]   # 11:01 UTC, en hora de Madrid como el motivo
    assert "72 % en 9 noticias distintas (36 historias×moneda) · a 4 h desde el triaje, ajustado al mercado" in r["trace"]
    assert "con «6 diarias», 6 entradas y 3 % de riesgo; con E2, el motor cuenta también estas entradas en su tope de 4 y 2 %" in r["trace"]
    assert 'data-story="n1"' in r["res"] and 'data-story="nf-n1-btc"' not in r["res"]
    assert 'data-story="n1"' in r["cl"]
    _sin_null(r["trace"], r["res"], r["cl"])


def test_motor_puesto_por_el_usuario_y_vigilancia_sin_la_regla_del_paper():
    """Con «6 diarias» activada por scripts/activar_estrategia.py: fuente «usuario» rotulada como tu decisión (no como
    la pestaña Evolución), y ninguna vigilada sale como «la regla que opera el paper»."""
    r = run(FIAB + VIGIL + """H.open(); reset();
      H.recv(opn({...SEIS_P, preset: "seis", nombre: "6 diarias"}, {params_source: "usuario",
        estrategia_cargada: {firma: "ab12", ts: now - 60e3, pausada: false, fuente: "usuario", registro: "v1-usuario-seis", preset: "seis"}}));
      S.tab = "market"; renderAll(true); const mk = H.el("#openings").innerHTML;
      H.recv(vig({estrategias: [{...E2, en_paper: true}]})); renderVigil(); const v = H.el("#vigil").innerHTML;
      H.recv(opn({preset: "impulso"})); renderVigil(); const v2 = H.el("#vigil").innerHTML;
      return {mk, v, v2};""")
    assert "Tu decisión" in r["mk"] and "scripts/activar_estrategia.py" in r["mk"]
    assert "aplicada por tu decisión (scripts/activar_estrategia.py)" in r["mk"] and "aplicada desde la pestaña Evolución" not in r["mk"]
    assert "(usuario)" not in r["mk"] and "la regla que opera el paper" not in r["v"] and "la regla que opera el paper" in r["v2"]
    _sin_null(r["mk"], r["v"], r["v2"])


def test_demostracion_una_posicion_por_moneda_y_horquilla_pura():
    """En la demostración las operaciones normales también cumplen tus reglas: una posición por moneda, el máximo de
    posiciones de config (4), stop ≥ 1,5 % y horquilla pura (sin break-even)."""
    r = run("""S.positions.length = 0; S.scheduled.length = 0; S.pending.length = 0; S.execStop = false;
      S.positions.push({id: "ya", coin: "BTC", side: "long", qty: 1, entry: 1, sl: 0.9, tp1: null, tp2: 2, notional: 1, riskUsd: 0.1, opened: S.vnow, be: false, bracket: true, maxAge: 36e5});
      emit({kind: "captured", story: "dos", title: "Prueba", coins: ["BTC"], patch: {headline: "Prueba", src: "x", tier: "T1", t0: S.vnow, coins: ["BTC"]}});
      S.stories.get("dos").strat = {coin: "BTC", side: "long", mode: "x", risk: 0.5, stopPct: 1.5, timing: "now", tp1R: 3, tp2R: 3, bracket: true};
      riskStage("dos"); const risk = S.stories.get("dos").risk;
      return {ok: risk && risk.ok, why: risk && risk.why, max: S.limits.max_positions};""", live=False)
    assert r["ok"] is False and "Una sola posición por moneda: ya hay una en BTC" in r["why"] and r["max"] == 4
    page = HTML.read_text(encoding="utf-8")
    assert "Math.max(1.5, 1.5*MKT.hours" in page and "Stop movido al precio de entrada" not in page   # sin break-even
    assert "tp1R:3, tp2R:3, tp1Frac:0, bracket:true, maxAgeMs:60*60000" in page


def test_la_curva_de_capital_va_sobre_la_cartera_de_prueba_y_no_sobre_10000():
    """En vivo la curva dice el nombre y los euros de la cartera de prueba; en la demostración, 100 € (111,37 USDC) y
    nunca la cuenta de 10.000 de antes."""
    vivo = run("""H.open(); reset();
      H.recv(pf({equity: 111.37, start_equity: 111.37, cuenta: {nombre: "Cartera de prueba de 100 €", eur: 100, eur_usdc: 1.1137, usdc: 111.37}}));
      S.tab = "trades"; renderTrades(); return {n: H.el("#eqNote").textContent};""")
    assert "Cartera de prueba de 100 €: 100 € ficticios (111,37 USDC al empezar)" in vivo["n"] and "10.000" not in vivo["n"]
    demo = run("""S.tab = "trades"; renderTrades();
      return {n: H.el("#eqNote").textContent, aviso: H.el("#avisoDemo").hidden};""", live=False)
    assert "100 € ficticios" in demo["n"] and "10.000" not in demo["n"] and "10000" not in demo["n"]
    assert demo["aviso"] is False   # la copia publicada avisa de que son datos de ejemplo


def test_revision_28_10_operacion_por_noticia_capital_sin_cartera_y_grupo():
    """Revisión del 28-10: la operación de la entrada por noticia (nf-…, tier «Plan») no es un plan de apertura «sin
    noticia»; en directo, sin cartera del bot, no se enseña el capital de la demostración; el grupo de Resultados sale
    con su nombre (tags.grupo_nombre), no con la clave."""
    r = run("""H.open(); reset(); S.tab = "trades"; renderAll(true);
      const kpis = H.el("#kpis").innerHTML, eq = H.el("#equity").innerHTML;
      H.recv(ev("captured", "n1", {headline: "Titular de la noticia", src: "rbc.ru", tier: "T2", t0: now - 60e3, coins: ["BTC"]}),
             ev("entrada_noticia", "n1", {entrada_noticia: {estado: "confirmada", motivo: "ok", moneda: "BTC", dir: 1,
               paper_story: "nf-n1-btc"}}),
             ev("strategy", "nf-n1-btc", {headline: "Noticia fiable · Compra BTC", src: "Entrada por noticia fiable", tier: "Plan",
               t0: now, noticia: "n1", strat: strat("BTC", "long", {origen: "noticia_fiable", noticia: "n1",
               tags: {origen: "noticia_fiable", story: "n1"}})}));
      S.sel = "nf-n1-btc"; renderTrace(); const op = H.el("#trace").innerHTML;
      S.sel = "n1"; renderTrace(); const news = H.el("#trace").innerHTML;
      const tr = {id: "t1", story: "nf-n1-btc", strategy: "noticia-fiable-largo", window: "Noticia fiable", market: "", coin: "BTC",
        side: "long", qty: 1, entry: 100, exit: 98.5, opened: now - 3600e3, closed: now, reason: "Stop", pnl: -0.5, pnl_pct: -1.5,
        R: -1, fees: 0.1, headline: "h", instrument: "spot", funding: 0, commissions: 0.1,
        tags: {origen: "noticia_fiable", story: "n1", grupo: "tipo_sentido:regulatorio:-1", grupo_nombre: "regulatorio · bajista"}};
      S.results = {start_equity: 111.37, equity: 110.87, trades: [tr], by_strategy: [], by_window: [], by_coin: [],
                   equity_curve: [[now - 7200e3, 111.37], [now, 110.87]], daily: []};
      S.tab = "results"; renderAll(true);
      return {kpis, eq, op, news, res: H.el("#resRows").innerHTML};""")
    assert "111,37" not in r["kpis"] and "esperando la cartera del bot" in r["kpis"] and "Sin cartera del bot" in r["eq"]
    assert "sin noticia que lo dispare" not in r["op"] and "Disparada por una noticia fiable" in r["op"]
    assert 'data-story="n1"' in r["op"] and "Titular de la noticia" in r["op"]
    assert 'data-story="nf-n1-btc"' in r["news"] and "Abrir la operación (paper)" in r["news"]
    assert "grupo fiable regulatorio · bajista" in r["res"] and "tipo_sentido:regulatorio" not in r["res"]
    _sin_null(r["op"], r["news"], r["res"])


def test_contadores_del_cazador_con_los_totales_reales():
    """Hallazgo n.º 8: con la foto (pocos descartes) la web daba que pasaba un 79 %; el mensaje «conteo» manda."""
    r = run("""H.open(); reset();
      H.recv(ev("captured", "a", {headline: "x"}), ev("captured", "b", {headline: "y"}), ev("filtered", "c", {headline: "z"}),
             {type: "conteo", desde: now - 3600e3, kinds: {captured: 1420, filtered: 2462, corroborated: 3, triaged: 302, lowprio: 50}});
      const a = {cap: S.counts.captured, fil: S.counts.filtered};
      H.recv(ev("captured", "d", {headline: "w"}));
      return {...a, cap2: S.counts.captured};""")
    assert r == {"cap": 1420, "fil": 2462, "cap2": 1421}


def test_evolucion_ensena_postmortems_y_acierto_por_dimension():
    """Hallazgos n.º 16 y 21: la web pinta los post-mortems (resumen sin IA, texto de la IA local y estado) y el acierto
    por nivel, región, idioma, tipo, ventana, fase, rol y moneda con su intervalo y los casos independientes."""
    r = run("""H.open(); reset();
      H.recv({type: "aprendizaje", ts: now, estado: "ok", params: {min_casos: 20, min_dias: 10},
        por_nivel: [{clave: "T1", casos: 400, independientes: 60, dias: 12, acierto: 0.522, ic90: [0.47, 0.57], bruto: 0.41, base: 0.39},
                    {clave: "T3 <x>", casos: 30, independientes: 5, dias: 2, acierto: 0.61, ic90: [0.4, 0.8], bruto: 0.5, base: 0.4}],
        por_moneda: [{clave: "BTC", casos: 900, independientes: 80, dias: 14, acierto: 0.49, ic90: [0.45, 0.53], bruto: 0.4, base: 0.41}],
        postmortems: [{id: "p:t1", origen: "paper", story: "a-europa-btc-1", moneda: "BTC", cerrada: now - 3600e3, R: -1.04,
                       resumen: "Stop a las 11:02 <b>", texto: null, estado: "pendiente", modelo: null, hechos: {}},
                      {id: "v:9", origen: "vigilancia", story: "v9", moneda: "SOL", cerrada: now - 7200e3, R: 3.0,
                       resumen: "Objetivo", texto: "Ganó porque el impulso siguió.", estado: "hecho", modelo: "Qwen3.5 9B (local)", hechos: {}}]});
      S.tab = "evo"; renderEvo(true);
      const nivel = H.el("#aprDim").innerHTML, chips = H.el("#aprDimF").innerHTML, pm = H.el("#evoPM").innerHTML;
      S.aprDim = "moneda"; renderEvo(true);
      return {nivel, chips, pm, moneda: H.el("#aprDim").innerHTML};""")
    assert "T1" in r["nivel"] and "52 %" in r["nivel"] and "(47 %–57 %)" in r["nivel"] and "T3 &lt;x&gt;" in r["nivel"]
    assert "poca muestra" in r["nivel"] and 'data-apr-dim="region"' in r["chips"] and "BTC" in r["moneda"]
    assert "Stop a las 11:02 &lt;b&gt;" in r["pm"] and "Pendiente de la IA local" in r["pm"] and "-1,04 R" in r["pm"]
    assert "Ganó porque el impulso siguió." in r["pm"] and "Explicado" in r["pm"] and "virtual (vigilancia)" in r["pm"]
    assert 'data-story="a-europa-btc-1"' in r["pm"] and 'data-story="v9"' not in r["pm"]
    _sin_null(r["nivel"], r["pm"], r["moneda"])
