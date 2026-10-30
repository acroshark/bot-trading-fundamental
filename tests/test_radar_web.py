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
    r = run("""for (const t of ["live", "world", "mando", "pred", "trades", "results", "ranking", "real", "bt", "lab", "market", "evo", "ai"]) H.fire("click", {"data-tab": t});
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
      const a = {{live: H.el("#btLive").innerHTML + H.el("#btLiveKpis").innerHTML, lab: H.el("#btLab").innerHTML + H.el("#btLabKpis").innerHTML}};
      S.btImp = {{impulso: null, lab: null}}; renderBtLive();
      return {{...a, vacio: H.el("#btLive").innerHTML, vacioLab: H.el("#btLab").innerHTML}};""", live=False)
    live, lb = r["live"], r["lab"]
    assert "+0,35 R" in live and "202 op." in live and "+69,88 R" in live and "≈ +34,9 % de la cuenta" in live
    assert "No es una ventaja demostrada" in live and "+0,14 R en 278 operaciones" in live
    assert "La cuenta honesta en avance pierde" in live and "-61,2 %" in live and "65,3 %" in live
    assert "+0,53 R" in live and "+0,10 R" in live and "Variante elegida &lt;b&gt;después&lt;/b&gt;" in live
    assert "E2 &lt;i&gt;" in lb and "la opera el motor" in lb and "<small>Probadas</small><b>2000" in lb and "lista congelada el 2026-10-26" in lb
    assert "<b>68 de 1358</b><span>por puro azar se esperan unas 68" in lb
    assert "<b>Comisiones del backtest:</b> 0,10 % por lado y 2 pb de deslizamiento" in lb   # también el laboratorio lo dice
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
    muestra pierde y está en la cartera por tu decisión) y E2 pasa a «Estrategia anterior». Con «impulso» o, con el bot,
    sin dato, E2 sigue en marcha y «6 diarias» sale debajo como medida y no en marcha. (En la demostración sin dato manda
    la estrategia activa simulada: test_backtest_seis2_en_marcha_y_seis_como_anterior.)"""
    page = HTML.read_text(encoding="utf-8")
    r = run("const now = Date.now();" + FIAB + f"""S.btImp = {{impulso: {json.dumps(IMP_BT)}, lab: null, seis: {json.dumps(SEIS_BT)}}};
      const out = {{}};
      for (const [k, m] of [["seis", opn({{...SEIS_P, preset: "seis", nombre: "6 diarias"}})], ["impulso", opn({{preset: "impulso"}})], ["nada", null]]){{
        S.openings = m; S.live = k === "nada"; S.tab = "bt"; S.btSig = null; renderAll(true); S.live = false;
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
    for x in (i, n):
        assert x["title"] == "Estrategia en marcha: impulso de los primeros minutos (E2)" and x["sub"].startswith("Lo que opera ahora")
        assert x["top"] is True and x["alt"] is False and "Si la activas, será por tu decisión" in x["seis"] and "-0,12 R" in x["seis"]
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
    assert "Espera hasta" in r["trace"] and "11:01:00 UTC" in r["trace"]
    assert "72 % en 9 noticias distintas (36 historias×moneda) · a 4 h desde el triaje, ajustado al mercado" in r["trace"]
    assert "con «6 diarias» y «6 diarias v2», 6 entradas y 3 % de riesgo; con E2, el motor cuenta también estas entradas en su tope de 4 y 2 %" in r["trace"]
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


# Por qué entra o no (tabla «decisiones_apertura» del bot: GET /api/decisiones y mensaje «decision_apertura»), 28-10-2026.
# T0: Tokio hace 3 h; AYER, Europa 24 h antes; CHINA, 26 h antes (perdida con el bot parado); VIEJA, hace 4 días (no sale).
DEC = r"""
const dia = t => new Date(t).toLocaleDateString("sv-SE", {timeZone: "Europe/Madrid"});
const T0 = now - 3 * 3600e3, AYER = T0 - 864e5, CHINA = T0 - 26 * 3600e3, VIEJA = now - 4 * 864e5, M15 = 15 * 60e3;
let ID = 0;
const fila = (o = {}) => { const ap = o.apertura_ms !== undefined ? o.apertura_ms : T0, v = o.ventana || "tokio-seul";
  return {id: ++ID, ts: (ap || now) + M15, ventana_id: `${v}-${dia(ap || now)}`, ventana: v, dia: dia(ap || now), apertura_ms: ap, coin: "BTC",
    fase: "medida", decision: "entra", motivo_codigo: "entra", motivo: "Impulso +0,32 % en los 15 primeros min", impulso_pct: 0.32, lado: "largo",
    preset: "seis", estrategia: "6 diarias", trade_id: null, story: null, datos: null, ...o}; };
const dec = o => ({type: "decision_apertura", ts: now, row: fila(o)});
// Tokio: BTC y ETH entran (BTC ya abierta en el paper), SOL la rechaza el paper (exposición) y XRP no entra (impulso nulo)
const TOKIO = [
  fila({coin: "BTC"}), fila({coin: "BTC", fase: "entrada", ts: T0 + M15 + 2000, motivo: "Se envía al paper"}),
  fila({coin: "BTC", fase: "paper", decision: "abierta", ts: T0 + M15 + 3000, trade_id: "tabc123", story: "a-tok-btc-1", motivo: "Abierta en el paper",
        datos: {plan: {entrada: 84000.5, stop: 82740.5}, nulo: null, lista: [1, null, 2], hora: T0, ok: true}}),
  fila({coin: "ETH", impulso_pct: -0.21, lado: "corto"}),
  fila({coin: "ETH", fase: "entrada", ts: T0 + M15 + 2000, impulso_pct: -0.21, lado: "corto", motivo: "Se envía al paper"}),
  fila({coin: "SOL", impulso_pct: 0.4}),
  fila({coin: "SOL", fase: "paper", decision: "rechazada", motivo_codigo: "exposicion", ts: T0 + M15 + 3000, story: "a-tok-sol-1", estrategia: "6 diarias <i>",
        motivo: "La exposición subiría al 133 % <b>", datos: '{"limites": {"exposicion_pct": 100, "posiciones": 3}, "x": null}'}),
  fila({coin: "XRP", decision: "no_entra", motivo_codigo: "impulso_nulo", motivo: null, impulso_pct: 0, lado: null})];
const EUROPA = [
  fila({ventana: "europa", apertura_ms: AYER, coin: "BTC", fase: "cierre", decision: "cerrada", motivo_codigo: "stop", ts: AYER + 5 * 3600e3,
        trade_id: "tbtc0", motivo: "Tocó el stop", datos: {R: -1.07}}),
  fila({ventana: "europa", apertura_ms: AYER, coin: "ETH", fase: "cierre", decision: "cerrada", motivo_codigo: "objetivo", ts: AYER + 9 * 3600e3,
        trade_id: "teth0", motivo: "Llegó al objetivo", datos: {R: 2.86}}),
  fila({ventana: "europa", apertura_ms: AYER, coin: "SOL", fase: "riesgo", decision: "rechazada", motivo_codigo: "perdida_dia", motivo: "Pérdida del día 3 % <i>"}),
  fila({ventana: "europa", apertura_ms: AYER, coin: "XRP", fase: "entrada", decision: "no_entra", motivo_codigo: "tope_dia", motivo: "Tope de 6 entradas"})];
const PERDIDA = [fila({ventana: "china", apertura_ms: CHINA, coin: "BTC", fase: "perdida", decision: "perdida", motivo_codigo: "bot_parado",
                       ts: CHINA + 3 * 3600e3, motivo: null, impulso_pct: null, lado: null})];
const VIEJAS = [fila({ventana: "saopaulo", apertura_ms: VIEJA, coin: "BTC"})];
const decHtml = () => H.el("#decBox").innerHTML;
"""


def test_decisiones_por_la_api_agrupadas_por_apertura_con_recuento_y_mando():
    """Al conectar se pide GET /api/decisiones; el panel «Por qué entra o no» agrupa por apertura (la más reciente arriba)
    y por moneda con impulso, chip de la decisión, motivo, hora y enlace a la operación; debajo, el recuento del día; en
    Mando, la línea corta de la última apertura. Lo de más de 3 días no sale; todo escapado y sin null."""
    r = run(DEC + """const ROWS = [...TOKIO, ...EUROPA, ...PERDIDA, ...VIEJAS];
      H.fetchReply = url => url.startsWith("/api/decisiones") ? {ok: true, json: async () => ({rows: ROWS.slice().reverse(), n: ROWS.length, desde: now - 3 * 864e5})} : {ok: false};
      H.open(); reset();
      H.recv(ev("strategy", "a-tok-btc-1", {headline: "Apertura Tokio · Compra BTC", src: "Motor de aperturas", tier: "Plan", t0: T0, coins: ["BTC"], strat: strat()}),
             ev("strategy", "a-tok-sol-1", {headline: "Apertura Tokio · Compra SOL", src: "Motor de aperturas", tier: "Plan", t0: T0, coins: ["SOL"], strat: strat("SOL")}));
      await loadApi();
      S.tab = "market"; renderAll(true); const box = decHtml();
      S.tab = "mando"; renderAll(true);
      return {box, md: H.el("#mdDec").innerHTML, f: H.fetches.slice(), n: S.dec.length, api: S.decApi};""")
    box, md = r["box"], r["md"]
    assert any(u.startswith("/api/decisiones?desde=") and "limite=2000" in u for u in r["f"]) and r["api"] == "ok"
    assert r["n"] == 14
    # agrupación: la más reciente arriba; lo de hace 4 días no sale
    i_t, i_e, i_c = box.index("Tokio y Seúl"), box.index("<b>Europa</b>"), box.index("Shanghái y Hong Kong")
    assert i_t < i_e < i_c and "São Paulo" not in box and box.count('rowspan="4"') == 2 and 'rowspan="1"' in box
    # cada decisión con su chip
    for chip in ("Abierta (paper)", ">Entra<", "Rechazada por el paper", ">No entra<", "Cerrada: stop · -1,07 R", "Cerrada: objetivo · +2,86 R",
                 "Rechazada por el riesgo", "Perdida con el bot parado"):
        assert chip in box, chip
    # impulso, motivo en llano (el del bot o el del catálogo), recorrido por fases y datos sin null
    assert "+0,32 %" in box and "-0,21 %" in box and "▼ corto" in box and "0,00 %" in box
    assert "La exposición subiría al 133 % &lt;b&gt;" in box and "Pérdida del día 3 % &lt;i&gt;" in box and "133 % <b>" not in box
    assert "sin dirección no se entra" in box and "pasó con el bot parado" in box and "6 diarias &lt;i&gt;" in box
    assert "Medida: entra" in box and "Entrada: entra" in box and "Paper: abierta" in box and "Paper: rechazada" in box
    assert "plan.entrada: 84.000,5" in box and "lista: 1, 2" in box and "ok: sí" in box and "limites.exposicion_pct: 100" in box
    # enlace a la operación: expediente si la web lo tiene; si no, Resultados
    assert 'data-story="a-tok-btc-1"' in box and "tabc123" in box and 'data-story="a-tok-sol-1"' in box and ">Expediente<" in box
    assert 'data-tabgo="results"' in box and "teth0" in box
    # recuento del día de la apertura más reciente
    assert "Recuento de" in box and "4 apertura×moneda" in box
    assert '<span class="pill good">Entran 2</span>1 abierta' in box and '<span class="pill muted">No entran 1</span>impulso nulo 1' in box
    assert '<span class="pill bad">Rechazadas 1</span>exposición (paper) 1' in box
    # entradas por noticia, aparte (aquí ninguna)
    assert "Entradas por noticia fiable" in box and "Ninguna en los 3 últimos días" in box
    # Mando
    assert "Tokio: entra en BTC y ETH; SOL rechazada (exposición); XRP no entra (impulso nulo)" in md and "Última apertura" in md
    assert "Ejemplo" not in md
    _sin_null(box, md)


def test_decisiones_catalogo_de_motivos_en_lenguaje_llano():
    """Cada motivo_codigo del catálogo cerrado (docs/04) se explica en llano cuando la fila no trae su texto, y sale con
    su nombre corto en el recuento y en Mando; uno desconocido no rompe nada."""
    r = run(DEC + """H.open(); reset();
      const B = Date.parse(dia(now) + "T10:00:00Z");   // todas el mismo día de Madrid: un solo recuento
      const CODES = ["impulso_nulo", "impulso_bajo", "tarde", "sin_velas", "veto_noticia", "tope_dia", "riesgo_dia", "perdida_dia", "exposicion",
                     "moneda_ocupada", "max_posiciones", "reglas", "aprobacion", "sin_contexto", "otro", "zzz"];
      H.recv(...CODES.map((k, i) => dec({ventana: "w" + i, coin: "BTC", fase: "entrada", decision: k === "exposicion" ? "rechazada" : "no_entra",
        motivo_codigo: k, motivo: null, apertura_ms: B - i * 60e3})));
      H.recv(...["objetivo", "stop", "tiempo"].map((k, i) => dec({ventana: "c" + i, coin: "ETH", fase: "cierre", decision: "cerrada", motivo_codigo: k,
        motivo: null, apertura_ms: B - (20 + i) * 60e3})));
      S.tab = "market"; renderAll(true); const box = decHtml();
      S.tab = "mando"; renderAll(true);
      return {box, md: H.el("#mdDec").innerHTML, lines: decWindows().slice(0, 3).map(decLine)};""")
    box = r["box"]
    for t in ("sin dirección no se entra", "no llegó al mínimo para entrar", "Ya había pasado la hora de entrada", "No llegaron las velas de 1 min",
              "se veta la entrada", "todas las entradas permitidas hoy", "presupuesto de riesgo del día", "pérdida máxima del día",
              "pasaría del 100 % del capital", "una posición por moneda", "el máximo de posiciones", "No cumple tus reglas",
              "No se aprobó a tiempo", "Llegó al objetivo.", "Tocó el stop.", "plazo máximo de la operación", "Sin motivo.",
              "sin que llegaran de Binance los datos para decidir"):
        assert t in box, t
    for t in ("impulso nulo 1", "impulso bajo 1", "tarde para entrar 1", "sin velas 1", "noticia fiable en contra 1", "tope de entradas del día 1",
              "riesgo del día agotado 1", "pérdida máxima del día 1", "moneda ocupada 1", "máximo de posiciones 1", "tus reglas 1",
              "sin aprobación 1", "sin contexto 1", "otro motivo 1", "sin motivo 1", "exposición 1"):
        assert t in box, t
    assert "Cerrada: objetivo" in box and "Cerrada: stop" in box and "Cerrada: fin del plazo" in box
    assert r["lines"][0].endswith("BTC no entra (impulso nulo)") and r["lines"][1].endswith("BTC no entra (impulso bajo)")
    assert r["lines"][2].endswith("BTC no entra (tarde para entrar)") and "BTC no entra (impulso nulo)" in r["md"]
    _sin_null(box, r["md"], *r["lines"])


def test_decisiones_por_websocket_404_reinicio_y_campos_rotos():
    """Un bot sin GET /api/decisiones (404) no rompe nada y se avisa; cada «decision_apertura» que llega se añade (sin
    duplicar la misma fila) y la última de cada apertura×moneda manda; tras un reinicio siguen; abrir la pestaña vuelve a
    pedir la ruta; filas rotas o sin moneda no rompen nada."""
    r = run(DEC + """H.open(); reset(); await loadApi();
      S.tab = "market"; renderAll(true); const vacio = decHtml();
      S.tab = "mando"; renderAll(true); const md0 = H.el("#mdDec").innerHTML;
      const NY = now - 5 * 60e3, ny = o => dec({ventana: "nuevayork", apertura_ms: NY, ts: NY, fase: "medida", decision: "esperando", motivo_codigo: "otro",
        motivo: "Midiendo el impulso de los 15 primeros minutos", impulso_pct: null, lado: null, ...o});
      const a = ny({coin: "BTC"}), b = ny({coin: "ETH"});
      H.recv(a, b); renderAll(true); const md1 = H.el("#mdDec").innerHTML;
      H.recv(a); const n1 = S.dec.length;
      H.recv(ny({coin: "BTC", ts: NY + M15, fase: "entrada", decision: "entra", motivo_codigo: "entra", motivo: "Se envía al paper", impulso_pct: 0.12, lado: "largo"}));
      renderAll(true); const md2 = H.el("#mdDec").innerHTML;
      reset(); const n2 = S.dec.length;
      H.fetches.length = 0; H.fire("click", {"data-tab": "market"}); const f = H.fetches.slice();
      H.recv({type: "decision_apertura"}, {type: "decision_apertura", row: null}, {type: "decision_apertura", row: {coin: "BTC"}},
             {type: "decision_apertura", row: 7}, dec({ts: "x"}), dec({coin: null}),
             dec({ventana: "europa", apertura_ms: AYER, coin: "SOL", fase: "cierre", decision: "cerrada", motivo_codigo: "objetivo", motivo: null,
                  datos: '{"R": 1.5, "x": null, "y": [null, {"z": null}]}'}),
             dec({ventana: "europa", apertura_ms: AYER, coin: "ETH", fase: "cierre", decision: "cerrada", motivo_codigo: "stop", motivo: null,
                  datos: '{"R": NaN}'}),
             dec({ventana: "europa", apertura_ms: AYER, coin: "XRP", fase: "zzz", decision: "rara", motivo_codigo: "zzz", motivo: null, impulso_pct: "x",
                  lado: "arriba", datos: "no es JSON", estrategia: 5, preset: null, trade_id: 9, story: {a: 1}}),
             dec({ventana: null, ventana_id: "suelta", apertura_ms: null, dia: null, coin: "DOGE", decision: null, fase: null, motivo_codigo: null}));
      S.tab = "market"; renderAll(true); const rotas = decHtml();
      S.tab = "mando"; renderAll(true);
      return {vacio, md0, md1, md2, n1, n2, f, rotas, md3: H.el("#mdDec").innerHTML, api: S.decApi};""")
    assert r["api"] == "fallo"
    assert "No se pudo leer GET /api/decisiones" in r["vacio"] and "Aún no ha llegado ninguna decisión" in r["vacio"]
    assert "Aún no hay decisiones guardadas" not in r["vacio"] and "sobrevive" not in r["vacio"]   # sin la ruta, nada «guardado»
    assert "sin decisiones guardadas" in r["md0"]
    assert "Nueva York: BTC y ETH esperan (midiendo el impulso)" in r["md1"]
    assert r["n1"] == 2 and "Nueva York: entra en BTC; ETH espera (midiendo el impulso)" in r["md2"]
    assert r["n2"] == 3   # un reinicio del bot no borra lo guardado
    assert any(u.startswith("/api/decisiones?") for u in r["f"])
    rotas = r["rotas"]
    assert "Cerrada: objetivo · +1,50 R" in rotas and "Llegó al objetivo." in rotas and ">rara<" in rotas and "Sin motivo." in rotas
    assert ">Cerrada: stop</span>" in rotas   # un JSON que no se puede leer (NaN) se queda sin datos, sin romper nada
    assert "DOGE" in rotas and "suelta" in rotas and "Sin decisión" in rotas
    _sin_null(r["vacio"], r["md0"], r["md1"], r["md2"], rotas, r["md3"])


def test_decisiones_sin_la_ruta_404_no_dice_que_el_bot_las_guarda():
    """Un bot en marcha sin GET /api/decisiones (404) no guarda estas decisiones: la web lo dice así y no afirma en ningún
    sitio que sobrevivan a los reinicios. Solo cuando la ruta responde dice que vienen de lo guardado en el bot."""
    r = run(DEC + """H.fetchReply = url => url.startsWith("/api/decisiones") ? {ok: false, status: 404} : {ok: false};
      H.open(); reset(); await loadApi();
      S.tab = "market"; renderAll(true); const vacio = decHtml();
      S.tab = "mando"; renderAll(true); const md0 = H.el("#mdDec").innerHTML;
      H.recv(...TOKIO.map(x => ({type: "decision_apertura", ts: now, row: x})));
      S.tab = "market"; renderAll(true); const conWs = decHtml(), api404 = S.decApi;
      H.fetchReply = url => url.startsWith("/api/decisiones") ? {ok: true, json: async () => ({rows: TOKIO, n: TOKIO.length, desde: now - 3 * 864e5})} : {ok: false};
      await loadDec(); renderAll(true);
      return {vacio, md0, conWs, api404, ok: decHtml(), api: S.decApi};""")
    assert r["api404"] == "sin_ruta" and r["api"] == "ok"
    assert "El bot no lo guarda" in r["vacio"] and "aún no guarda estas decisiones" in r["vacio"] and "no hay nada que enseñar" in r["vacio"]
    assert "Aún no hay decisiones guardadas" not in r["vacio"] and "el motor escribe una" not in r["vacio"] and "sobrevive" not in r["vacio"]
    assert "el bot en marcha aún no guarda estas decisiones" in r["md0"]
    assert "se pierde al reiniciarlo" in r["conWs"] and "Tokio y Seúl" in r["conWs"] and "sobrevive" not in r["conWs"]
    assert "Guardado en el bot" in r["ok"] and "sobrevive a los reinicios" in r["ok"] and "El bot no lo guarda" not in r["ok"]
    # la cabecera fija del panel no afirma que el bot lo guarde (eso depende de que responda la ruta)
    page = HTML.read_text(encoding="utf-8")
    cab = page[page.index("<h2>Por qué entra o no</h2>"):page.index('id="decBox"')]
    assert "sobrevive" not in cab and "decisiones_apertura" not in cab and "Solo paper." in cab
    _sin_null(r["vacio"], r["md0"], r["conWs"], r["ok"])


def test_decisiones_entradas_por_noticia_aparte():
    """Las entradas por noticia fiable (ventana «noticia», ventana_id «noticia-<story>») van en su propia tabla con el
    titular escapado y el enlace al expediente; no cuentan en el recuento de aperturas ni en la línea de Mando."""
    r = run(DEC + """H.open(); reset();
      H.recv(ev("captured", "n1", {headline: "Titular <i>", src: "rbc.ru", tier: "T2", t0: now - 600e3, coins: ["SOL"]}));
      const nt = (story, o) => dec({ventana: "noticia", ventana_id: "noticia-" + story, apertura_ms: null, dia: dia(now), story, estrategia: "Entrada por noticia fiable", ...o});
      H.recv(...TOKIO.map(x => ({type: "decision_apertura", ts: now, row: x})),
             nt("n1", {coin: "SOL", ts: now - 300e3, fase: "entrada", decision: "entra", motivo_codigo: "entra", motivo: "SOL se movió +0,71 % <b>", impulso_pct: 0.71}),
             nt("n2", {coin: "XRP", ts: now - 200e3, fase: "entrada", decision: "no_entra", motivo_codigo: "tope_dia", motivo: null, impulso_pct: null, lado: null}));
      S.tab = "market"; renderAll(true); const box = decHtml();
      S.tab = "mando"; renderAll(true);
      return {box, md: H.el("#mdDec").innerHTML};""")
    box = r["box"]
    ap, nt = box.split("Entradas por noticia fiable")
    assert "Titular" not in ap and "4 apertura×moneda" in ap
    assert "Titular &lt;i&gt;" in nt and "Titular <i>" not in nt and 'data-story="n1"' in nt and "SOL se movió +0,71 % &lt;b&gt;" in nt
    assert "Noticia n2" in nt and "todas las entradas permitidas hoy" in nt and "+0,71 %" in nt
    assert r["md"].count("Tokio:") == 1 and "SOL se movió" not in r["md"]
    _sin_null(box, r["md"])


def test_decisiones_demostracion_seis_diarias_marcada_como_ejemplo():
    """Sin bot: decisiones de EJEMPLO de los 3 últimos días con la lógica de la estrategia activa simulada, «6 diarias v2»
    (≤ 6 entradas al día, una posición por moneda, exposición ≤ 100 %: como mucho 4 a la vez con stop 2 %; la mitad en
    3R y el resto con cerrojo), rotuladas como ejemplo, sin estrategias que ya no existen y sin pedir nada al bot."""
    r = run("""const out = {};
      for (const [k, t] of [["noche", Date.UTC(2026, 9, 28, 21, 0)], ["midiendo", Date.UTC(2026, 9, 28, 0, 5)]]){
        S.vnow = t; S.decDemoK = null; S.tab = "market"; renderAll(true); const box = H.el("#decBox").innerHTML;
        S.tab = "mando"; renderAll(true);
        const rows = S.dec.filter(x => x.ventana !== "noticia"), ab = {}, abiertas = rows.filter(x => x.decision === "abierta");
        for (const x of abiertas) ab[x.dia] = (ab[x.dia] || 0) + 1;
        const fin = id => { const c = rows.find(x => x.trade_id === id && x.decision === "cerrada"); return c ? c.ts : Infinity; };
        const iv = abiertas.map(x => ({c: x.coin, a: x.ts, b: fin(x.trade_id)}));
        const maxA = Math.max(0, ...iv.map(x => iv.filter(y => y.a <= x.a && y.b > x.a).length));
        const dup = iv.some(x => iv.some(y => y !== x && y.c === x.c && y.a <= x.a && y.b > x.a));
        out[k] = {box, md: H.el("#mdDec").innerHTML, demo: S.decDemo, n: rows.length, ab, maxA, dup,
          presets: [...new Set(rows.map(x => x.preset))], estr: [...new Set(rows.map(x => x.estrategia))],
          rech: [...new Set(rows.filter(x => x.decision === "rechazada").map(x => x.motivo_codigo))],
          no: [...new Set(rows.filter(x => x.decision === "no_entra").map(x => x.motivo_codigo))],
          decs: [...new Set(rows.map(x => x.decision))], tid: abiertas.every(x => /^t[0-9a-f]{12}$/.test(x.trade_id)),
          plan: rows.filter(x => x.datos && x.datos.plan).every(x => x.datos.plan.stop_pct === 2 && x.datos.plan.primer_objetivo_R === 3 && x.datos.plan.fraccion === 0.5),
          parc: rows.filter(x => x.decision === "parcial").every(x => x.trade_id && rows.some(y => y.trade_id === x.trade_id && y.decision === "abierta" && y.ts < x.ts)),
          dias: [...new Set(rows.map(x => x.dia))].sort()}; }
      return {...out, fetches: H.fetches.length, sockets: H.sockets.length};""", live=False)
    a, b = r["noche"], r["midiendo"]
    assert a["demo"] is True and a["n"] > 40 and a["dias"] == ["2026-10-26", "2026-10-27", "2026-10-28"]
    assert a["presets"] == ["seis2"] and a["estr"] == ["6 diarias v2"] and a["tid"] and a["plan"] and a["parc"]
    assert all(v <= 6 for v in a["ab"].values()) and a["maxA"] <= 4 and a["dup"] is False
    assert set(a["rech"]) <= {"exposicion"} and set(a["no"]) <= {"impulso_nulo", "veto_noticia", "moneda_ocupada", "tope_dia"}
    assert {"entra", "abierta", "cerrada", "perdida"} <= set(a["decs"]) and "esperando" not in a["decs"]
    assert set(a["decs"]) <= {"entra", "abierta", "parcial", "cerrada", "perdida", "no_entra", "rechazada"}
    box = a["box"]
    assert "Ejemplo inventado" in box and "«6 diarias v2»" in box and "Perdida con el bot parado" in box and "Recuento de hoy (28/10" in box
    assert "stop 2 %, la mitad en 3R y el resto corre" in box and "(1,5 %)" not in box
    assert "E2" not in box and "ruptura" not in box.lower() and "Operación paper de ejemplo" in box and 'data-tabgo="results"' not in box
    assert "Última apertura" in a["md"] and "Ejemplo" in a["md"] and "Nueva York:" in a["md"]
    assert "esperando" in b["decs"] and "Tokio: BTC, ETH, SOL y XRP esperan (midiendo el impulso)" in b["md"]
    assert r["fetches"] == 0 and r["sockets"] == 0
    _sin_null(a["box"], a["md"], b["box"], b["md"])


# «6 diarias v2» (preset seis2 de bot/apertura.py, 29-10-2026): las entradas de «6 diarias» con la salida A (stop 2 %, la
# mitad en 3R y el resto con cerrojo: a +3R su stop sube a +2R y lo sigue a 2R por detrás; 24 h sin activar, 7 días activada).
SEIS2 = r"""
const SEIS2_P = {...SEIS_P, sl_pct: 2, target_r: null, tp1_r: 3, tp1_frac: 0.5, lock_r: 3, floor_r: 2, trail_r: 2, trail_atr: null,
  max_hold_h: 24, max_hold_active_h: 168, avg_hold_h: 16, preset: "seis2", nombre: "6 diarias v2"};
const M = 60e3;
const colsOf = html => Object.fromEntries(html.split('<div class="col">').slice(1).map(c => [c.split("</h3>")[0].split("<h3>")[1], c]));
"""
SEIS2_BT = {"generated": 1793230000000, "split": 1755295200000, "sl_pct": 2.0, "nota": "Réplica del motor, salida A <b>", "nombre": "6 diarias v2",
            "params": {**SEIS_BT["params"], "sl_pct": 2.0, "target_r": None, "tp1_r": 3.0, "tp1_frac": 0.5, "lock_r": 3.0, "floor_r": 2.0,
                       "trail_r": 2.0, "trail_atr": None, "max_hold_h": 24.0, "max_hold_active_h": 168.0},
            "fuera": {"n": 1462, "mean_R": -0.0754, "win_rate": 0.3824, "max_R": 5.7, "paper_R_funding": -0.0749,
                      "salidas": {"stop": 684, "tiempo": 601, "parcial": 177, "cerrojo": 150, "seguimiento": 25, "tiempo (activada)": 2},
                      "horas_media": 17.1, "largos": {"n": 750, "mean_R": -0.15}, "cortos": {"n": 712, "mean_R": 0.004},
                      "desde": 1755476100000, "hasta": 1792505400000, "curva_R": [[1755480600000, -1.12], [1792505400000, -110.2]]},
            "dentro": {"n": 2354, "mean_R": -0.0543, "win_rate": 0.3785, "desde": 1698624900000, "hasta": 1755328500000,
                       "curva_R": [[1698646140000, -1.12], [1755328500000, -127.8]]}}


def test_tablero_programadas_y_pasando_controles_del_motor():
    """«Estrategias y cartera» con el estado del motor: en Programadas, las aperturas que aún no han abierto (hora de
    Madrid, estrategia activa y qué hará); en Pasando controles, las que miden o esperan la entrada con lo que va midiendo
    cada moneda. Las terminadas no salen. Avisos de pausa y de tope del día; E2 con sus filtros; escapado y sin null."""
    r = run(FIAB + SEIS2 + """H.open(); reset();
      S.tab = "trades"; renderAll(true); const sin = colsOf(H.el("#kanban").innerHTML);
      const T1 = now + 3*3600e3 + 20*M, T0 = now - 8*M;
      const esp = why => ({signal: 0, strength: 0, state: "esperando_apertura", why, last_why: why, levels: null, tags: null, plan: null});
      const wins = [
        {id: "china-1", name: "Shanghái <b>", markets: [], open_utc: T1, state: "esperando_apertura", why: "", coins: {BTC: esp("A las 02:30 Madrid se mide"), ETH: esp(""), SOL: esp(""), XRP: esp("")}},
        {id: "tokio-1", name: "Tokio y Seúl", markets: [], open_utc: T0, state: "midiendo_impulso", why: "", coins: {
          BTC: {signal: 0, state: "midiendo_impulso", last_why: "Midiendo el impulso <i>", levels: {apertura: 100, impulso_pct: null, movimiento_pct: 0.12, minutos_medidos: 8}},
          ETH: {signal: 0, state: "midiendo_impulso", last_why: null, why: null, levels: {movimiento_pct: -0.21, minutos_medidos: 8}},
          SOL: {signal: 1, state: "esperando_entrada", step: "hora", last_why: "Impulso de +0,40 % en 15 min", levels: {impulso_pct: 0.4}, tags: {impulso_pct: 0.4}},
          XRP: {signal: 0, state: "terminada", last_why: "Impulso de 0,00 %: sin dirección no se entra", levels: {impulso_pct: 0}}}},
        {id: "europa-0", name: "Europa vieja", markets: [], open_utc: now - 20*3600e3, state: "terminada", why: "", coins: {BTC: {state: "terminada"}}},
        null, {id: "rota", name: null, open_utc: "x", state: null, coins: null}];
      H.recv(opn(SEIS2_P, {params_source: "usuario", windows: wins, day: {date: madDay(T1), entries: 2, risk_pct: 1, max_entries: 6, max_risk_pct: 3}}));
      renderAll(true); const a = colsOf(H.el("#kanban").innerHTML), st = H.el("#tradesStrat").innerHTML;
      H.recv(opn(SEIS2_P, {paused: true, windows: wins, day: {date: madDay(T1), entries: 6, risk_pct: 3, max_entries: 6, max_risk_pct: 3}}));
      renderAll(true); const b = colsOf(H.el("#kanban").innerHTML);
      H.recv(opn({...SEIS_P, preset: "impulso", min_move_pct: 0.2, rvol_min: 1.5, btc_filter: "a_favor", entry: "t:30", sl_pct: 3, target_r: 3}, {windows: [wins[0]]}));
      renderAll(true); const e2 = colsOf(H.el("#kanban").innerHTML);
      return {sin, a, b, e2, st, h1: madHM(T1), he1: madHM(T1 + 15*M), he0: madHM(T0 + 15*M), h0: madHM(T0), he2: madHM(T1 + 30*M)};""")
    assert "aún no ha publicado su estado" in r["sin"]["Programadas"]
    p, c = r["a"]["Programadas"], r["a"]["Pasando controles"]
    assert '<span class="n">1</span>' in p and '<span class="n">1</span>' in c   # Shanghái (la rota, sin hora ni estado, no); Tokio midiendo
    assert "Shanghái &lt;b&gt;" in p and "Shanghái <b>" not in p and f"{r['h1']} Madrid · en 3 h" in p and 'data-tabgo="market"' in p
    assert (f"Medirá el impulso de los 15 primeros min de BTC, ETH, SOL y XRP y entrará a las {r['he1']} en el sentido del impulso; "
            "stop 2 %, la mitad en 3R y el resto corre") in p and "6 diarias v2 · paper" in p
    assert "Tokio y Seúl" in c and f"abrió a las {r['h0']} Madrid" in c and f"midiendo el impulso de los 15 primeros min; entrará a las {r['he0']}" in c
    assert "+0,12 %</b> <span class=\"note\">en 8 min" in c and "Midiendo el impulso &lt;i&gt;" in c and "-0,21 %" in c
    assert '<span class="pos">▲</span> <b class="mono pos">+0,40 %</b>' in c and "Esperando la entrada · Impulso de +0,40 % en 15 min" in c
    assert "Terminada · Impulso de 0,00 %" in c and "Europa vieja" not in p + c
    assert "6 diarias v2" in r["st"] and "preset 6 diarias v2" in r["st"] and "Puesta por tu decisión" in r["st"]
    pb = r["b"]["Programadas"] + r["b"]["Pasando controles"]
    assert "Motor pausado: no abrirá entradas" in pb and "Tope de entradas del día alcanzado" in r["b"]["Programadas"]
    assert (f"entrará a las {r['he2']} en el sentido del impulso si pasa de ±0,20 % con volumen relativo ≥ 1,5 con BTC a favor; "
            "stop 3 % y objetivo 3R") in r["e2"]["Programadas"]
    _sin_null(*r["sin"].values(), *r["a"].values(), *r["b"].values(), *r["e2"].values(), r["st"])


def test_tablero_posiciones_y_cerradas_sin_expediente():
    """Tras conectar (o tras un reinicio) el paper puede tener posiciones y operaciones cerradas sin su expediente en la
    web: el tablero también las enseña (En posición y Cerradas), con su R y el enlace a la noticia si es por noticia fiable."""
    r = run(FIAB + SEIS2 + """H.open(); reset();
      H.recv(pf({positions: [{story: "a-x-btc-1", coin: "BTC", side: "long", qty: 1, entry: 100, sl: 98, riskUsd: 0.5, notional: 25, upnl: 0.3,
                 headline: "Apertura Tokio · Compra BTC <i>", strategy: "impulso-largo"}],
               closed: [{story: "a-y-eth-1", id: "t1", coin: "ETH", side: "short", entry: 100, exit: 102, pnl: -0.56, R: -1.12, reason: "Stop de pérdida",
                         closed: now - 3600e3, headline: "Venta ETH"},
                        {story: "nf-n1-sol", id: "t2", coin: "SOL", side: "long", entry: 100, exit: 104.5, pnl: 1.5, R: 3, reason: "Objetivo",
                         closed: now - 7200e3, headline: "Titular", tags: {origen: "noticia_fiable", story: "n1"}}]}));
      S.tab = "trades"; renderAll(true); return colsOf(H.el("#kanban").innerHTML);""")
    pos, cer = r["En posición"], r["Cerradas o descartadas"]
    assert '<span class="n">1</span>' in pos and "Apertura Tokio · Compra BTC &lt;i&gt;" in pos and "+0,30 USDC · 0,60 R" in pos and 'data-story="a-x-btc-1"' in pos
    assert '<span class="n">2</span>' in cer and "-1,12 R · Stop de pérdida" in cer and "+3,00 R · Objetivo" in cer
    assert 'data-story="n1"' in cer and ">Noticia fiable</span>" in cer and cer.index("ETH") < cer.index("SOL")
    _sin_null(pos, cer)


def test_tablero_en_la_demostracion_con_la_estrategia_activa_simulada():
    """Sin bot, el tablero y el motor de Mercado usan el motor SIMULADO con la estrategia activa («6 diarias v2»):
    aperturas reales de las próximas 24 h en Programadas y, si una está midiendo, en Pasando controles; rotulado como
    ejemplo, con los mismos impulsos que «Por qué entra o no» y sin pedir nada al bot."""
    r = run("""const colsOf = html => Object.fromEntries(html.split('<div class="col">').slice(1).map(c => [c.split("</h3>")[0].split("<h3>")[1], c]));
      const out = {};
      for (const [k, t] of [["midiendo", Date.UTC(2026, 9, 28, 0, 8)], ["noche", Date.UTC(2026, 9, 28, 21, 0)]]){
        S.vnow = t; S.decDemoK = null; S.demoMotor = null; S.tab = "trades"; renderAll(true);
        const kb = colsOf(H.el("#kanban").innerHTML), st = H.el("#tradesStrat").innerHTML;
        S.tab = "market"; renderAll(true);
        const O = motorO(), tok = O.windows.find(w => w.open_utc === Date.UTC(2026, 9, 28, 0, 0));
        out[k] = {kb, st, mk: H.el("#openings").innerHTML, sub: H.el("#openSub").textContent, pre: motorPreset().preset, demo: O.demo,
          n: O.windows.length, tok: tok ? Object.fromEntries(Object.entries(tok.coins).map(([c, x]) => [c, [x.state, x.levels && x.levels.impulso_pct]])) : null}; }
      // el impulso que mide el tablero acaba siendo el de la decisión de ejemplo
      S.vnow = Date.UTC(2026, 9, 28, 0, 15, 30); S.demoMotor = null; S.decDemoK = null;
      const fin = Object.fromEntries(Object.entries(motorO().windows.find(w => w.open_utc === Date.UTC(2026, 9, 28, 0, 0)).coins).map(([c, x]) => [c, x.levels.impulso_pct]));
      S.vnow = Date.UTC(2026, 9, 28, 1, 0); S.decDemoK = null; demoDecRefresh();
      const dec = Object.fromEntries(S.dec.filter(x => x.ventana === "tokio-seul" && x.dia === "2026-10-28" && x.fase === "medida").map(x => [x.coin, x.impulso_pct]));
      return {...out, fin, dec, fetches: H.fetches.length, sockets: H.sockets.length};""", live=False)
    a, b = r["midiendo"], r["noche"]
    assert a["pre"] == "seis2" and a["demo"] is True and r["fetches"] == 0 and r["sockets"] == 0
    pa, ca = a["kb"]["Programadas"], a["kb"]["Pasando controles"]
    assert "Tokio y Seúl" in ca and "Midiendo el impulso" in ca and "en 8 de 15 min" in ca and ">Ejemplo</span>" in ca
    assert "entrará a las 01:15 en el sentido del impulso" in ca and all(v[0] == "midiendo_impulso" for v in a["tok"].values())
    assert "Shanghái y Hong Kong" in pa and "02:30 Madrid" in pa and ("Medirá el impulso de los 15 primeros min de BTC, ETH, SOL y XRP y "
                                                                     "entrará a las 02:45 en el sentido del impulso; stop 2 %, la mitad en 3R y el resto corre") in pa
    for w in ("Europa", "São Paulo", "Nueva York"):
        assert w in pa, w
    assert "6 diarias v2" in a["st"] and ">Ejemplo</span>" in a["st"]
    assert "Ejemplo inventado" in a["mk"] and "Midiendo el impulso" in a["mk"] and a["sub"].startswith("Modo impulso «6 diarias v2»")
    # el motor simulado es un ejemplo, no «la estrategia activa» (el bot puede tener otra, p. ej. «6 diarias 4R»)
    assert "con «6 diarias v2» como ejemplo" in a["mk"] and "con la estrategia activa" not in a["mk"]
    pb, cb = b["kb"]["Programadas"], b["kb"]["Pasando controles"]
    assert "Ninguna apertura midiendo ahora." in cb and "01:00 Madrid" in pb and "Tokio y Seúl" in pb and b["n"] == 5
    assert r["fin"] == r["dec"] and len(r["dec"]) == 4
    _sin_null(*a["kb"].values(), *b["kb"].values(), a["mk"], b["mk"], a["st"], b["st"])


def test_preset_seis2_en_mercado_y_cartera():
    """Con p.preset «seis2» la web la nombra «6 diarias v2», explica su salida en llano (la mitad en 3R, el resto con
    cerrojo a +2R desde +3R y seguimiento a 2R; sin break-even antes de +3R; 24 h o 7 días) y la marca como tu decisión."""
    r = run(FIAB + SEIS2 + """H.open(); reset();
      H.recv(opn(SEIS2_P, {params_source: "usuario"}));
      S.btImp = {seis2: {fuera: {n: 1462, mean_R: -0.0754}}};
      S.tab = "market"; renderAll(true); const mk = H.el("#openings").innerHTML, sub = H.el("#openSub").textContent;
      S.tab = "trades"; renderAll(true); const st = H.el("#tradesStrat").innerHTML;
      S.tab = "results"; renderAll(true);
      return {mk, sub, st, res: H.el("#resStrat").innerHTML};""")
    assert r["sub"].startswith("Modo impulso «6 diarias v2» (paper, por tu decisión)")
    assert ("con stop 2 %; la mitad sale en 3R (+6 %) y la otra mitad corre: al llegar a +3R su stop sube a +2R y lo sigue a 2R por "
            "detrás. Sin break-even: antes de +3R el stop no se mueve; como mucho 24 h si no llega a +3R y 7 días si llega.") in r["sub"]
    for t in ("stop 2,00 %", "la mitad sale en 3R (+6 %)", "la otra mitad corre: al llegar a +3R su stop sube a +2R y lo sigue a 2R por detrás",
              "como mucho 24 h si no llega a +3R y 7 días si llega", "sin break-even: antes de +3R el stop no se mueve", "Tu decisión"):
        assert t in r["mk"], t
    assert "sin break-even ni stop dinámico" not in r["mk"] and "objetivo" not in r["mk"].split("Parámetros")[1].split("</p>")[0]
    for h in (r["st"], r["res"]):
        assert "6 diarias v2" in h and "preset 6 diarias v2" in h and "Puesta por tu decisión" in h
        assert "stop 2 %, la mitad en 3R y el resto corre" in h and "-0,08 R por operación fuera de muestra" in h
    _sin_null(r["mk"], r["sub"], r["st"], r["res"])


def test_backtest_seis2_en_marcha_y_seis_como_anterior():
    """Con «6 diarias v2» en marcha, la pestaña Backtest la abre arriba (pierde menos que «6 diarias», pero pierde, y tu
    regla se mantiene) y «6 diarias» y E2 pasan a anteriores. Con «seis» o «impulso», la v2 sale debajo como medida. En
    la demostración sin dato manda la simulada (v2); sin resumen se avisa. El incrustado real (seis2Data) se pinta igual."""
    page = HTML.read_text(encoding="utf-8")
    r = run("const now = Date.now();" + FIAB + SEIS2 + f"""S.btImp = {{impulso: {json.dumps(IMP_BT)}, lab: null, seis: {json.dumps(SEIS_BT)}, seis2: {json.dumps(SEIS2_BT)}}};
      const out = {{}};
      for (const [k, m] of [["seis2", opn(SEIS2_P)], ["seis", opn({{...SEIS_P, preset: "seis"}})], ["impulso", opn({{preset: "impulso"}})], ["demo", null]]){{
        S.openings = m; S.tab = "bt"; S.btSig = null; renderAll(true);
        const t2 = !H.el("#btSeis2TopP").hidden, t1 = !H.el("#btSeisTopP").hidden;
        out[k] = {{title: H.el("#btLiveTitle").textContent, sub: H.el("#btLiveSub").textContent, top2: t2, alt2: !H.el("#btSeis2AltP").hidden,
          top1: t1, alt1: !H.el("#btSeisAltP").hidden, altTitle: H.el("#btSeisAltTitle").textContent,
          s2: (t2 ? H.el("#btSeis2Top") : H.el("#btSeis2Alt")).innerHTML + H.el("#btSeis2Kpis").innerHTML, s1: (t1 ? H.el("#btSeisTop") : H.el("#btSeisAlt")).innerHTML}}; }}
      S.btImp = {{impulso: null, lab: null, seis: null, seis2: null}}; S.openings = opn(SEIS2_P); S.btSig = null; renderAll(true);
      out.vacio = {{top2: !H.el("#btSeis2TopP").hidden, html: H.el("#btSeis2Top").innerHTML, alt1: !H.el("#btSeisAltP").hidden}};
      return out;""", live=False)
    v2, s, i, d = r["seis2"], r["seis"], r["impulso"], r["demo"]
    assert v2["top2"] and not v2["alt2"] and not v2["top1"] and v2["alt1"] and v2["altTitle"] == "Estrategia anterior: 6 diarias"
    assert v2["title"].startswith("Estrategia anterior (E2)") and "«6 diarias v2»" in v2["sub"]
    assert "Estuvo en tu cartera paper por tu decisión desde el 28-10-2026 hasta que pasaste a «6 diarias v2»" in v2["s1"]
    h = v2["s2"]
    for t in ("Fuera de muestra pierde", "-0,08 R por operación en 1462 operaciones", "(dentro de muestra -0,05 R)", "por jornada de lunes a viernes",
              "Con las mismas entradas, «6 diarias» da -0,12 R por operación fuera de muestra", "pierde menos (+0,04 R), pero sigue perdiendo",
              "la mayoría de las operaciones no llega a +3R", "mitad en el primer objetivo 177", "cerrojo 150", "Tu regla se mantiene",
              "sin break-even: antes de +3R el stop no se mueve", "La mitad sale en 3R (+6 %)", "esa mitad gana como poco +2R (+4 % bruto)",
              "La mejor operación fuera de muestra da +5,70 R", "Está en tu cartera paper por tu decisión", "Réplica del motor, salida A &lt;b&gt;",
              "<small>Fuera de muestra</small>", "mejor +5,70 R", "con funding -0,07 R", "-110,20 R"):
        assert t in h, t
    assert s["top1"] and not s["top2"] and s["alt2"] and "Si la activas, será por tu decisión" in s["s2"] and "«6 diarias»" in s["sub"]
    assert i["title"].startswith("Estrategia en marcha") and i["alt1"] and i["alt2"] and i["altTitle"].startswith("Medida en backtest")
    assert d["top2"] and d["title"].startswith("Estrategia anterior (E2)")   # la demostración opera la simulada (v2)
    assert r["vacio"]["top2"] and "no trae el backtest de «6 diarias v2»" in r["vacio"]["html"] and not r["vacio"]["alt1"]
    _sin_null(h, v2["s1"], s["s2"], r["vacio"]["html"])
    if 'id="seis2Data"' in page:   # el resumen real incrustado (scripts/incrustar_backtest.py) también se pinta sin huecos
        blob = page.split('<script type="application/json" id="seis2Data">', 1)[1].split("</script>", 1)[0]
        r2 = run("const now = Date.now();" + FIAB + SEIS2 + f"""S.btImp = {{impulso: null, lab: null, seis: null, seis2: {blob}}}; S.openings = opn(SEIS2_P); S.tab = "bt"; S.btSig = null; renderAll(true);
          return H.el("#btSeis2Top").innerHTML + H.el("#btSeis2Kpis").innerHTML;""", live=False)
        assert "Fuera de muestra" in r2 and "por tu decisión" in r2
        _sin_null(r2)


RUP3 = r"""
const RUP3_P = {...SEIS2_P, modelo: "ruptura3", entry: "brk", brk_tf: 5, entry_end_min: 240, news_mode: "corroboradas",
  rp_max_apertura: 2, rp_stop_max_pct: 3, preset: "ruptura3", nombre: "Ruptura V3: rango de apertura con tendencia, volumen y régimen"};
"""
RUP3_BT = {"generated": 1793260000000, "split": 1755295200000, "sl_pct": 2.0, "nota": "Réplica de la Ruptura V3 <b>",
           "nombre": "Ruptura V3: rango de apertura con tendencia, volumen y régimen",
           "params": {**SEIS2_BT["params"], "modelo": "ruptura3", "entry": "brk", "entry_end_min": 240, "news_mode": "corroboradas",
                      "rp_max_apertura": 2, "rp_stop_max_pct": 3.0},
           "fuera": {"n": 118, "mean_R": 0.4288, "win_rate": 0.5085, "paper_R_funding": 0.4285, "op_jornada": 0.386,
                     "salidas": {"stop": 50, "tiempo": 40, "cerrojo": 19, "seguimiento": 9}, "horas_media": 14.6,
                     "largos": {"n": 28, "mean_R": 0.1621}, "cortos": {"n": 90, "mean_R": 0.5114}, "desde": 1755476100000,
                     "hasta": 1792505400000, "curva_R": [[1755480600000, 1.0], [1792505400000, 50.6]],
                     "antes_de_costes": {"n": 118, "R_bruto": 0.5392, "deslizamiento_R": -0.0123, "comisiones_R": -0.0982,
                                         "funding_R": -0.0003, "R_neto": 0.4285, "costes_R": -0.1107, "gana_bruto_pct": 51.7,
                                         "op_jornada": 0.386, "cuenta_jornada_neto_pct": 0.0826}},
           "dentro": {"n": 159, "mean_R": -0.0331, "win_rate": 0.371, "desde": 1698624900000, "hasta": 1755328500000,
                      "curva_R": [[1698646140000, 6.8], [1755328500000, -5.25]],
                      "antes_de_costes": {"n": 159, "R_bruto": 0.0868, "deslizamiento_R": -0.023, "comisiones_R": -0.0969,
                                          "funding_R": 0.0, "R_neto": -0.033, "costes_R": -0.1198, "op_jornada": 0.34,
                                          "cuenta_jornada_neto_pct": -0.0056}}}


def test_ruptura3_nombre_descripcion_y_backtest():
    """Con el preset «ruptura3» la web la nombra («Ruptura V3»), explica qué hace y abre arriba su backtest: fuera de muestra
    gana, dentro no (no es una ventaja demostrada) y cuánto se llevan los costes. Con otra en marcha, sale como medida.
    El incrustado real (ruptura3Data) se pinta igual, sin huecos."""
    page = HTML.read_text(encoding="utf-8")
    r = run("const now = Date.now();" + FIAB + SEIS2 + RUP3 + f"""S.btImp = {{impulso: {json.dumps(IMP_BT)}, lab: null, seis: null, seis2: {json.dumps(SEIS2_BT)}, ruptura3: {json.dumps(RUP3_BT)}}};
      const out = {{}};
      for (const [k, m] of [["rup3", opn(RUP3_P, {{params_source: "usuario"}})], ["seis2", opn(SEIS2_P)]]){{
        S.openings = m; S.tab = "bt"; S.btSig = null; renderAll(true);
        out[k] = {{shown: !H.el("#btRup3P").hidden, title: H.el("#btRup3Title").textContent, html: H.el("#btRup3").innerHTML + H.el("#btRup3Kpis").innerHTML,
          e2: H.el("#btLiveTitle").textContent, e2sub: H.el("#btLiveSub").textContent, s2top: !H.el("#btSeis2TopP").hidden}};
        S.tab = "market"; renderAll(true); out[k].sub = H.el("#openSub").textContent;
        S.tab = "trades"; renderAll(true); out[k].st = H.el("#tradesStrat").innerHTML; }}
      S.btImp = {{impulso: null, lab: null, seis: null, seis2: null, ruptura3: null}}; S.openings = opn(RUP3_P); S.tab = "bt"; S.btSig = null; renderAll(true);
      out.vacio = {{shown: !H.el("#btRup3P").hidden, html: H.el("#btRup3").innerHTML}};
      return out;""", live=False)
    a, b = r["rup3"], r["seis2"]
    assert a["shown"] and a["title"].startswith("Estrategia en marcha: Ruptura V3") and not a["s2top"]
    assert a["e2"].startswith("Estrategia anterior (E2)") and "«Ruptura V3»" in a["e2sub"]
    for t in ("Fuera de muestra gana", "+0,43 R por operación en 118 operaciones", "Pero dentro de muestra pierde",
              "-0,03 R en 159 operaciones", "no es una ventaja demostrada", "Lo que se llevan los costes",
              "antes de costes +0,54 R por operación", "las comisiones -0,10 R", "quedan +0,43 R", "≈ 0,37 R por operación",
              "Tus reglas no se tocan", "Está en tu cartera paper por tu decisión", "Réplica de la Ruptura V3 &lt;b&gt;",
              "<small>Antes de costes</small>", "Ruptura V3: rango de los 15 primeros min", "stop 2,00-3,00 % (el otro lado del rango)"):
        assert t in a["html"], t
    assert a["sub"].startswith("Ruptura V3 (paper, por tu decisión)") and "como mucho 2 por apertura" in a["sub"]
    assert "Ruptura V3" in a["st"] and "preset Ruptura V3" in a["st"] and "+0,43 R por operación fuera de muestra" in a["st"]
    assert "Dentro de muestra no gana" in a["st"]
    assert b["shown"] and b["title"].startswith("Medida en backtest, no está en marcha: Ruptura V3") and b["s2top"]
    assert "Si la activas (scripts/activar_estrategia.py --preset ruptura3)" in b["html"]
    assert r["vacio"]["shown"] and "no trae el backtest de la Ruptura V3" in r["vacio"]["html"]
    _sin_null(a["html"], b["html"], a["sub"], a["st"], r["vacio"]["html"])
    if 'id="ruptura3Data"' in page:   # el resumen real incrustado (scripts/incrustar_backtest.py)
        blob = page.split('<script type="application/json" id="ruptura3Data">', 1)[1].split("</script>", 1)[0]
        r2 = run("const now = Date.now();" + FIAB + SEIS2 + RUP3 + f"""S.btImp = {{impulso: null, lab: null, seis: null, seis2: null, ruptura3: {blob}}}; S.openings = opn(RUP3_P); S.tab = "bt"; S.btSig = null; renderAll(true);
          return H.el("#btRup3").innerHTML + H.el("#btRup3Kpis").innerHTML;""", live=False)
        assert "Fuera de muestra gana" in r2 and "Lo que se llevan los costes" in r2 and "por tu decisión" in r2
        _sin_null(r2)


def test_posicion_a_medias_en_cartera_resultados_y_expediente():
    """Una posición con la mitad ya cerrada en 3R y el resto con el stop del cerrojo se ve así en el tablero, en
    «Abiertas ahora» de Resultados, en Mando y en el expediente (con el R total, nunca como «break-even»); una cerrada con
    parcial enseña el precio medio, la parte y el resto, y el R de cada una."""
    r = run(FIAB + SEIS2 + """H.open(); reset();
      H.recv(opn(SEIS2_P));
      const pos = {story: "a-tok-btc-1", id: "tp1", coin: "BTC", side: "long", qty: 0.5, entry: 100, sl: 104, sl_initial: 98, trail_pct: null, high_water: 107,
        opened: now - 5*3600e3, riskUsd: 0.5, notional: 25, upnl: 1.73, headline: "Apertura Tokio · Compra BTC <b>", strategy: "impulso-largo-sin-noticia",
        window: "Tokio y Seúl", price: 106.5, R: 3.46, tp2: null, be: true, hold_until: now + 6*864e5, tp1: 106, tp1_frac: 0.5, tp1_done: true, realized: 0.72,
        be_r: null, trail_start_r: null, instrument: "spot", funding: 0, tags: {origen: "motor_aperturas", preset: "seis2"},
        lock_r: 3, floor_r: 2, lock_trail_r: 2, locked: true, floor: 104, hold_active: now + 6*864e5, sl_kind: "cerrojo"};
      const pos2 = {...pos, story: "a-tok-eth-1", id: "tp2", coin: "ETH", qty: 1, sl: 98, tp1_done: false, realized: 0, upnl: -0.1, locked: false,
        sl_kind: "inicial", price: 99.8, headline: "Compra ETH", hold_until: now + 864e5};
      const tr = {id: "t9", story: "a-eu-sol-1", strategy: "impulso-corto", window: "Europa", market: "", coin: "SOL", side: "short", qty: 1, entry: 100, exit: 95,
        sl_initial: 102, opened: now - 30*3600e3, closed: now - 3600e3, reason: "Stop del cerrojo tras salida parcial", pnl: 1.17, pnl_pct: 4.68, R: 2.34, fees: 0.06,
        riskUsd: 0.5, notional: 25, headline: "Venta SOL", instrument: "perp", funding: 0, commissions: 0.06, exit_last: 96,
        partials: [{qty: 0.5, px: 94, at: now - 20*3600e3, pnl: 0.74, fee: 0.02, why: "Objetivo parcial (3,0 R)"}],
        tags: {origen: "motor_aperturas", preset: "seis2"}, lock_r: 3, floor_r: 2, locked: true};
      H.recv(pf({positions: [pos, pos2], closed: [{...tr, closedAt: tr.closed}]}));
      S.results = {start_equity: 111.37, equity: 112.5, trades: [tr], by_strategy: [], by_window: [], by_coin: [],
                   equity_curve: [[now - 7200e3, 111.37], [now, 112.5]], daily: []};
      H.recv(ev("strategy", "a-tok-btc-1", {headline: "Apertura Tokio · Compra BTC <b>", src: "Motor de aperturas", tier: "Plan", t0: now - 5*3600e3, coins: ["BTC"],
               strat: strat("BTC", "long", {stopPct: 2, tp1R: 3, tp2R: null, tp1Frac: 0.5, beR: null, trailPct: null, trailStartR: null, lockR: 3, lockFloorR: 2,
                                            lockTrailR: 2, tags: {origen: "motor_aperturas", preset: "seis2"}})}),
             ev("riskok", "a-tok-btc-1", {risk: {checks: [], ok: true}}), ev("humanok", "a-tok-btc-1", {human: {mode: "auto", by: "Automático (paper)", at: now}}),
             ev("filled", "a-tok-btc-1", {exec: {px: 100, slip: 1, ms: 200, notional: 25, sl: 98, tp1: 106, tp2: null, tp1Frac: 0.5}}));
      S.tab = "trades"; renderAll(true); const kb = H.el("#kanban").innerHTML, cl = H.el("#closedRows").innerHTML;
      S.tab = "results"; renderAll(true); const op = H.el("#resOpen").innerHTML, rows = H.el("#resRows").innerHTML;
      S.sel = "a-tok-btc-1"; renderTrace(); const trace = H.el("#trace").innerHTML;
      S.tab = "mando"; renderAll(true);
      const empty = (() => { H.recv(pf({positions: []})); renderResOpen(); return H.el("#resOpen").innerHTML; })();
      return {kb, cl, op, rows, trace, md: H.el("#mdBook").innerHTML, empty};""")
    kb, op, tr = r["kb"], r["op"], r["trace"]
    assert ("+1,73 USDC · R total 3,46 R<br>La mitad cerrada en 3R (+0,72 USDC) · el resto corre con stop 104,00 (cerrojo: +2R desde la "
            "entrada; asegura ≈ +2,44 R en total)") in kb
    assert "R total -0,20 R<br>La mitad saldrá en 106,00 (3R) · stop 98,00" in kb and "break-even" not in kb
    assert "La mitad a 106,00 (3R): <b class=\"mono pos\">+0,72 USDC</b>" in op
    assert "La otra mitad con stop 104,00 (cerrojo activado: +2R desde la entrada; asegura ≈ +2,44 R en total) · sin objetivo: corre" in op
    assert "cierra como tarde el" in op and "+3,46 R" in op
    assert "Pendiente: la mitad saldrá en 106,00 (3R)" in op and "-0,20 R" in op and op.count("<tr ") == 2
    assert "Ninguna posición abierta ahora." in r["empty"]
    for h in (r["rows"], r["cl"]):
        assert "media: la mitad a 94,00 · resto a 96,00" in h and "parcial +1,48 · resto +0,86" in h
    assert "+2,34<br>" in r["cl"] and "Stop del cerrojo tras salida parcial" in r["rows"]
    for t in ("La mitad sale en 3R (+6 %)", "La otra mitad corre: al llegar a +3R su stop sube a +2R y lo sigue a 2R por detrás",
              "Sin break-even: antes de +3R el stop no se mueve", "106,00 (la mitad, 3R) · el resto corre con su cerrojo desde +3R",
              "(R total 3,46 R) · la mitad cerrada en 3R (+0,72 USDC)"):
        assert t in tr, t
    assert "stop ya en break-even" not in tr and "Horquilla pura" not in tr and "sin tope: stop dinámico" not in tr
    assert "La mitad ya cobrada · R total +3,46 R · Apertura Tokio · Compra BTC &lt;b&gt;" in r["md"]
    _sin_null(kb, r["cl"], op, r["rows"], tr, r["md"], r["empty"])


def test_decisiones_salida_parcial_y_cierre_con_cerrojo():
    """«Por qué entra o no» con la salida de «6 diarias v2»: la fila de la salida parcial como la escribe bot/decisiones.py
    (fase «cierre», decisión «parcial», motivo «objetivo»: sigue abierta, con su R) y los cierres por el cerrojo (o el
    seguimiento) con su nombre corto y su R; cuentan como entradas y salen en Mando."""
    r = run(DEC + """H.open(); reset();
      const B = Date.parse(dia(now) + "T10:00:00Z");
      H.recv(dec({ventana: "europa", apertura_ms: B, coin: "BTC", fase: "paper", decision: "abierta", motivo_codigo: "entra", trade_id: "tq1", ts: B + M15 + 3000}));
      H.recv(dec({ventana: "europa", apertura_ms: B, coin: "BTC", fase: "cierre", decision: "parcial", motivo_codigo: "objetivo", motivo: null, trade_id: "tq1",
                  datos: {R: 1.44, fraccion: 0.5, cerrojo: true}, ts: B + 3*3600e3}),
             dec({ventana: "europa", apertura_ms: B, coin: "ETH", fase: "cierre", decision: "cerrada", motivo_codigo: "cerrojo", motivo: null, trade_id: "tq2",
                  datos: {R: 2.45}, ts: B + 9*3600e3}),
             dec({ventana: "europa", apertura_ms: B, coin: "SOL", fase: "cierre", decision: "cerrada", motivo_codigo: "seguimiento", motivo: null,
                  datos: {R: 3.1}, ts: B + 10*3600e3}));
      S.tab = "market"; renderAll(true); const box = decHtml();
      S.tab = "mando"; renderAll(true);
      return {box, md: H.el("#mdDec").innerHTML};""")
    box, md = r["box"], r["md"]
    assert "Salida parcial · +1,44 R · sigue abierta" in box and "Cerrada: cerrojo · +2,45 R" in box and "Cerrada: seguimiento · +3,10 R" in box
    assert "Salió una parte en el primer objetivo" in box and "Llegó al objetivo." not in box
    assert "stop del cerrojo" in box and "stop que seguía al precio" in box and "Cierre: salida parcial" in box
    assert '<span class="pill good">Entran 3</span>1 abierta · 2 cerradas' in box
    assert "BTC a medias (la mitad cobrada)" in md and "ETH cerrada (cerrojo +2,45 R)" in md and "SOL cerrada (seguimiento +3,10 R)" in md
    _sin_null(box, md)


# ---------------------------------------------------------------- remate de «6 diarias v2»: lo que encontró la revisión de la web

_LBL = re.compile(r'<text x="([\d.]+)" y="[-\d.]+" text-anchor="end"[^>]*>([^<]*)</text>')


def test_backtest_seis2_dice_que_el_cierre_por_tiempo_no_sigue_la_regla_y_el_eje_no_corta_el_menos():
    """La viñeta de la regla no afirma que cada parte que gana lo haga con ≥ 2R: el cierre de las 24 h sale con lo que
    haya, y se dice con las cuentas del resumen (424 de 567 ganadoras por debajo de 2R). «6 diarias» lo dice igual. Las
    etiquetas del eje de la curva caben enteras (el «-» de «-252,86 R» no se corta)."""
    bt2 = {**SEIS2_BT, "fuera": {**SEIS2_BT["fuera"], "ganadoras_n": 567, "ganadoras_bajo_2R": 424, "ganadoras_bajo_1R": 285,
                                 "curva_R": [[1755480600000, 13.78], [1770000000000, -119.54], [1792505400000, -252.86]]}}
    bt1 = {**SEIS_BT, "fuera": {**SEIS_BT["fuera"], "ganadoras_n": 542, "ganadoras_bajo_2R": 325, "ganadoras_bajo_1R": 220}}
    r = run("const now = Date.now();" + FIAB + SEIS2 + f"""S.btImp = {{impulso: null, lab: null, seis: {json.dumps(bt1)}, seis2: {json.dumps(bt2)}}};
      S.openings = opn(SEIS2_P); S.tab = "bt"; S.btSig = null; renderAll(true);
      const out = {{s2: H.el("#btSeis2Top").innerHTML, s1: H.el("#btSeisAlt").innerHTML, c2: H.el("#btSeis2Curve").innerHTML, c1: H.el("#btSeisCurve").innerHTML}};
      S.btImp = {{impulso: null, lab: null, seis: null, seis2: {json.dumps(SEIS2_BT)}}}; S.btSig = null; renderAll(true);
      out.sin = H.el("#btSeis2Top").innerHTML;
      return out;""", live=False)
    s2 = r["s2"]
    assert "Tu regla se mantiene en el stop, la parcial y el cerrojo" in s2 and "cada parte que gana lo hace con" not in s2
    assert ("<b>Pero no en el cierre por tiempo:</b> si no llega a +3R, a las 24 h se cierra a mercado con lo que haya, gane o "
            "pierda: fuera de muestra, 424 de las 567 ganadoras (75 %) ganaron menos de 2R (285 menos de 1R).") in s2
    assert "El tope de 24 h no sigue tu regla" in r["s1"] and "325 de las 542 ganadoras (60 %) ganaron menos de 2R" in r["s1"]
    assert ("Pero no en el cierre por tiempo:</b> si no llega a +3R, a las 24 h se cierra a mercado con lo que haya, gane o "
            "pierda.</span>") in r["sin"]
    for svg in (r["c2"], r["c1"]):
        labels = _LBL.findall(svg)
        assert len(labels) == 3 and any(t.startswith("-") for _, t in labels), labels
        for x, t in labels:   # 11 px de IBM Plex Mono: ≈ 6,6 px por carácter, anclado a la derecha en x
            assert float(x) - 6.6 * len(t) >= 0, (x, t)
    _sin_null(s2, r["s1"], r["sin"])


def test_tablero_usa_lo_que_publica_el_motor_hara_hora_y_entrada():
    """Programadas y Pasando controles cuentan lo que dice el motor (hara, hora_madrid, entrada_ms, estrategia), no un
    texto rehecho en la web: así no se separan con el primer ajuste. Sin esos campos (motor viejo), el de la web."""
    r = run(FIAB + SEIS2 + """H.open(); reset();
      const T1 = now + 2*3600e3, esp = {signal: 0, state: "esperando_apertura", why: "", last_why: ""};
      const w = {id: "china-1", name: "Shanghái", markets: [], open_utc: T1, state: "esperando_apertura", why: "", coins: {BTC: esp},
        hara: "medirá el impulso de los 15 primeros min de BTC si la volatilidad no es baja y entrará a las 23:59 <b>",
        hora_madrid: "09:41", estrategia: "Mi estrategia", entrada_ms: T1 + 20*M, medida_hasta: T1 + 15*M};
      H.recv(opn({...SEIS2_P, nombre: null, preset: null}, {windows: [w]}));
      S.tab = "trades"; renderAll(true); const a = colsOf(H.el("#kanban").innerHTML)["Programadas"];
      const w2 = {...w, open_utc: now - 5*M, state: "midiendo_impulso", entrada_ms: now + 15*M,
        coins: {BTC: {signal: 0, state: "midiendo_impulso", levels: {movimiento_pct: 0.1, minutos_medidos: 5}}}};
      H.recv(opn({...SEIS2_P, nombre: null, preset: null}, {windows: [w2]}));
      renderAll(true); const b = colsOf(H.el("#kanban").innerHTML)["Pasando controles"];
      H.recv(opn(SEIS2_P, {windows: [{...w, hara: null, hora_madrid: null, estrategia: null, entrada_ms: null}]}));
      renderAll(true); const c = colsOf(H.el("#kanban").innerHTML)["Programadas"];
      return {a, b, c, he: madHM(now + 15*M), he1: madHM(T1 + 15*M), h1: madHM(T1)};""")
    assert "Medirá el impulso de los 15 primeros min de BTC si la volatilidad no es baja y entrará a las 23:59 &lt;b&gt;" in r["a"]
    assert "09:41 Madrid · en " in r["a"] and "Mi estrategia · paper" in r["a"] and "la mitad en 3R" not in r["a"]
    assert f"entrará a las {r['he']} en el sentido del impulso" in r["b"] and "abrió a las 09:41 Madrid" in r["b"]
    assert (f"Medirá el impulso de los 15 primeros min de BTC y entrará a las {r['he1']} en el sentido del impulso; stop 2 %, "
            "la mitad en 3R y el resto corre") in r["c"] and f"{r['h1']} Madrid" in r["c"]
    _sin_null(r["a"], r["b"], r["c"])


def test_plan_de_seis2_dice_la_peor_parte_que_gana_y_cada_parte():
    """Con parcial y cerrojo, las cifras del plan son las de la peor parte que gana (el suelo del cerrojo), no un
    objetivo: se rotula así, se lista cada parte con su neto y no se escribe un «R:R 2,00» que parezca el objetivo."""
    r = run(FIAB + SEIS2 + """H.open(); reset();
      const plan = {stopPct: 2, tp2R: null, rr: 2, ok: true, why: "Desde 100 (contado)", instrument: "perp", gross_pct: 4, cost_pct: 0.24,
        funding_pct: 0.02, net_pct: 3.55, tp1R: 3, tp1Frac: 0.5, lockR: 3, lockFloorR: 2, lockTrailR: 2,
        partes: [{que: "parcial", R: 3, precio: 94, frac: 0.5, bruto_pct: 6, neto_pct: 5.74, funding_pct: 0.02, funding_h: 16},
                 {que: "suelo", R: 2, precio: 96, frac: null, bruto_pct: 4, neto_pct: 3.55, funding_pct: 0.21, funding_h: 168}],
        entry: 100, stop: 102, target: null, parcial: 94, suelo: 96};
      const w = {id: "tokio-1", name: "Tokio y Seúl", markets: [], open_utc: now - 30*M, state: "en_posicion", why: "",
        coins: {BTC: {signal: -1, state: "en_posicion", entries: 1, instrument: "perp", levels: {impulso_pct: -0.3},
                      tags: {impulso_pct: -0.3, lado: "corto", instrumento: "perp"}, plan}}};
      H.recv(opn(SEIS2_P, {windows: [w]}));
      S.tab = "market"; renderAll(true);
      return {mk: H.el("#openings").innerHTML, pt: planTxt(plan), pt1: planTxt({stopPct: 1.5, tp2R: 3, rr: 3, ok: true})};""")
    mk = r["mk"]
    assert "En posición (paper): peor parte que gana (suelo del cerrojo, 2R): neto 3,55 % (mínimo 3,0 %)" in mk
    assert ("Partes que ganan: la mitad: salida parcial en 3R, +6,00 % bruto y 5,74 % neto (funding en 16 h: −0,02 %) · "
            "suelo del cerrojo en 2R, +4,00 % bruto y 3,55 % neto (funding en 168 h: −0,21 %).") in mk
    assert "mínimo 2R por parte" in mk and "R:R 2,00" not in mk and "bruto +4,00 %" not in mk
    assert r["pt"] == "stop 2,00 % · la mitad en 3R y el resto corre · mínimo 2R por parte" and r["pt1"] == "stop 1,50 % · R:R 3,00"
    _sin_null(mk)


def test_demostracion_posiciones_y_cerradas_del_motor_simulado():
    """En la demostración, En posición y Cerradas traen las operaciones del motor simulado («6 diarias v2»), sacadas de
    las mismas filas de «Por qué entra o no» (abiertas, a medias y cerradas con su R), y una nota separa lo de las noticias."""
    r = run("""const colsOf = html => Object.fromEntries(html.split('<div class="col">').slice(1).map(c => [c.split("</h3>")[0].split("<h3>")[1], c]));
      S.vnow = Date.UTC(2026, 9, 28, 21, 0); S.decDemoK = null; S.demoMotor = null; S.tab = "trades"; renderAll(true);
      const kb = colsOf(H.el("#kanban").innerHTML), st = H.el("#tradesStrat").innerHTML, g = new Map();
      for (const x of S.dec) if (x.trade_id){ const s = g.get(x.trade_id) || new Set(); s.add(x.decision); g.set(x.trade_id, s); }
      const v = [...g.values()];
      return {kb, st, abiertas: v.filter(s => s.has("abierta") && !s.has("cerrada")).length, cerradas: v.filter(s => s.has("cerrada")).length,
        medias: v.filter(s => s.has("parcial") && !s.has("cerrada")).length};""", live=False)
    pos, cer = r["kb"]["En posición"], r["kb"]["Cerradas o descartadas"]
    assert r["abiertas"] > 0 and r["cerradas"] > 0
    assert pos.count("Motor «6 diarias v2»") == r["abiertas"] and pos.count("La mitad ya salió en 3R") == r["medias"]
    assert "Motor «6 diarias v2»" in cer and re.search(r"[-+]\d+,\d\d R · ", cer)
    assert ">Ejemplo</span>" in pos and "Ejemplo inventado: las tarjetas del motor" in r["st"]
    _sin_null(pos, cer, r["st"])


# --- Gráfica de cada posición abierta («En posición» y expediente) ---

GRAF = r"""
const T0 = now - 40*M;
const posA = {story: "a-tok-btc-1", id: "tq1", coin: "BTC", side: "long", qty: 0.25, entry: 100, sl: 98, sl_initial: 98, trail_pct: null, high_water: 101,
  opened: T0, riskUsd: 0.5, notional: 25, upnl: 0.2, headline: "Apertura Tokio · Compra BTC <b>", strategy: "impulso-largo", window: "Tokio y Seúl",
  price: 100.4, R: 0.4, tp2: null, be: false, hold_until: now + 864e5, hold_label: "tope de 24 h <i>", tp1: 106, tp1_frac: 0.5, tp1_done: false,
  realized: 0, be_r: null, trail_start_r: null, instrument: "spot", funding: 0, tags: {origen: "motor_aperturas", preset: "seis2"},
  lock_r: 3, floor_r: 2, lock_trail_r: 2, locked: false, floor: 104, hold_active: null, sl_kind: "inicial"};
const DESDE = Math.floor((T0 - 20*M)/M)*M;
const BARS = []; for (let t = DESDE; t < now - 2*M; t += M){ const p = 99.9 + 0.2*Math.sin(t/3e5); BARS.push([t, p, p + 0.15, p - 0.15, p + 0.05]); }
const velasOk = j => ({ok: true, json: async () => j});
const esperaVelas = async () => { await Promise.all(Object.values(S.velas).map(v => v.p)); };
const nVelas = () => H.fetches.filter(u => String(u).startsWith("/api/velas")).length;
"""


def test_grafica_de_cada_posicion_con_las_velas_del_bot_y_en_vivo():
    """En «En posición», justo debajo de cada operación abierta, su gráfica: velas de OKX EU del par que se operaría
    (GET /api/velas?fuente=okx, una petición por minuto) desde un poco antes de la entrada, el punto de la entrada, las líneas del stop, de la parcial en 3R y de la
    entrada, el precio de ahora y las cifras (ganancia de ahora en % y R, distancia al stop y a la parcial y lo que daría
    cada una). Se mueve con el precio en vivo; en el expediente sale más grande, con el suelo del cerrojo y el plazo."""
    r = run(FIAB + SEIS2 + GRAF + """H.open(); reset();
      H.fetchReply = url => url.startsWith("/api/velas") ? velasOk({coin: "BTC", tf: "1m", velas: [...BARS, [now, null, 1, 1, 1], "x"], hasta_datos: now - 2*M,
        fuente: "okx", mercado: "OKX EU", instId: "BTC-USDC"}) : {ok: false};
      H.recv({type: "market", prices: {BTC: 100.4}}, pf({positions: [posA]}));
      S.tab = "trades"; renderAll(true); const antes = colsOf(H.el("#kanban").innerHTML)["En posición"];
      await esperaVelas(); renderAll(true); renderAll(true);
      const kb = colsOf(H.el("#kanban").innerHTML)["En posición"], url = H.fetches.find(u => String(u).startsWith("/api/velas")), n1 = nVelas();
      H.recv({type: "market", prices: {BTC: 101}}); renderAll(true); const kb2 = colsOf(H.el("#kanban").innerHTML)["En posición"];
      H.recv(ev("strategy", "a-tok-btc-1", {headline: "Apertura Tokio · Compra BTC <b>", src: "Motor de aperturas", tier: "Plan", t0: T0, coins: ["BTC"],
               strat: strat("BTC", "long", {stopPct: 2, tp1R: 3, tp2R: null, tp1Frac: 0.5, beR: null, trailPct: null, trailStartR: null, lockR: 3, lockFloorR: 2,
                                            lockTrailR: 2, tags: {origen: "motor_aperturas", preset: "seis2"}})}),
             ev("riskok", "a-tok-btc-1", {risk: {checks: [], ok: true}}), ev("humanok", "a-tok-btc-1", {human: {mode: "auto", by: "Automático (paper)", at: now}}),
             ev("filled", "a-tok-btc-1", {exec: {px: 100, slip: 1, ms: 200, notional: 25, sl: 98, tp1: 106, tp2: null, tp1Frac: 0.5}}));
      S.sel = "a-tok-btc-1"; renderTrace(); const tr = H.el("#trace").innerHTML;
      return {antes, kb, kb2, tr, url, n1, desde: DESDE, he: madHM(T0), rects: [...kb.matchAll(/<rect /g)].length};""")
    kb, kb2, tr = r["kb"], r["kb2"], r["tr"]
    assert r["url"] == f"/api/velas?coin=BTC&desde={r['desde']}&tf=1m&fuente=okx" and r["n1"] == 1   # una sola petición por minuto
    assert "Cargando las velas de OKX EU, BTC-USDC…" in r["antes"] and '<span class="n">1</span>' in kb
    assert '<div class="pwrap"><button type="button" class="ticket" data-story="a-tok-btc-1">' in kb
    assert 'class="pchart" data-story="a-tok-btc-1" title="Abrir el expediente"' in kb and '<svg viewBox="0 0 244 156" role="img"' in kb
    assert "Apertura Tokio · Compra BTC &lt;b&gt;" in kb and "Compra BTC <b>" not in kb
    for t in ("SL 98,00", "3R 106,00", "Ent 100,00", ">100,40</text>", f">{r['he']}</text>", 'fill="var(--accent)"'):
        assert t in kb, t
    assert r["rects"] > 12   # las velas (más las zonas y la etiqueta del precio)
    assert '<small>Ahora</small><b class="pos">+0,40 %</b> <b class="pos">+0,40 R</b><span>100,40 · +0,20 USDC</span>' in kb
    assert '<small>Al stop</small><b>-2,39 %</b><span>daría <span class="neg">-1,00 R</span> · -0,50 USDC</span>' in kb
    assert '<small>A la parcial 3R</small><b>+5,58 %</b><span>sale la mitad: <span class="pos">+1,50 R</span> · +0,75 USDC</span>' in kb
    assert "Velas de OKX EU, BTC-USDC, de 3 min y precio en vivo · hora de Madrid" in kb and "del bot" not in kb
    assert '<b class="pos">+1,00 %</b>' in kb2 and ">101,00</text>" in kb2 and "<b>-2,97 %</b>" in kb2   # se mueve con el precio
    assert '<svg viewBox="0 0 540 220" role="img"' in tr and f"Entrada {r['he']} · 100,00" in tr
    for t in ("Parcial 3R 106,00", "Suelo +2R 104,00", "Stop 98,00", "Ahora 101,00", "al menos +2,50 R en total", "cierra como tarde el",
              "(tope de 24 h &lt;i&gt;)", "sin comisiones ni deslizamiento", "Velas de OKX EU, BTC-USDC, de 1 min"):
        assert t in tr, t
    assert 'data-close="a-tok-btc-1"' in tr
    _sin_null(r["antes"], kb, kb2, tr)


def test_grafica_sin_velas_del_bot_404_fallo_cerrojo_y_campos_rotos():
    """Sin la ruta (404, el bot aún no se ha reiniciado) o con la petición fallida, la gráfica sale igual con el precio en
    vivo y lo dice. Un corto con la mitad cobrada y el stop siguiendo al precio: stop en su sitio, «Al stop (sigue)» con la
    ganancia que asegura y lo cobrado. Una posición rota (moneda, entrada u hora no válidas, stop en la entrada) sale sin
    gráfica y sin romper nada; una sin expediente abre al hacer clic su ficha con la gráfica."""
    r = run(FIAB + SEIS2 + GRAF + """H.open(); reset();
      H.fetchReply = url => url.includes("coin=ETH") ? {ok: false, status: 404} : {ok: false};
      const corto = {...posA, story: "a-x-eth-1", id: "tq2", coin: "ETH", side: "short", entry: 100, sl: 95, sl_initial: 102, tp1: 94, tp1_done: true,
        realized: 0.75, upnl: 2.0, price: 93, locked: true, floor: 96, sl_kind: "seguimiento", opened: now - 30*3600e3, headline: "Venta ETH", hold_label: null};
      const sol = {...posA, story: "a-x-sol-1", id: "tq3", coin: "SOL", entry: 150, sl: 147, sl_initial: 147, tp1: 159, floor: 156, price: 150, upnl: 0, headline: "Compra SOL"};
      const rotas = [{story: "r1", coin: "DOGE", side: "long", entry: 1, sl: 0.9, opened: now, riskUsd: 0.5, qty: 1, notional: 1},
        {story: "r2", coin: "XRP", side: "long", entry: null, sl: 1, opened: "x", riskUsd: 0.5, qty: 1, notional: 1},
        {story: "r3", coin: "BTC", side: "long", entry: 100, sl: 100, opened: now, riskUsd: 0.5, qty: 1, notional: 1},
        {story: "r4", coin: "BTC", side: "long", entry: 100, sl_initial: 98, sl: 98, opened: now + 3600e3, riskUsd: 0.5, qty: 1, notional: 1, tp2: 106}];
      H.recv({type: "market", prices: {ETH: 93, SOL: 150, BTC: 100}}, pf({positions: [corto, sol, ...rotas]}));
      S.tab = "trades"; renderAll(true); await esperaVelas(); renderAll(true);
      const kb = colsOf(H.el("#kanban").innerHTML)["En posición"];
      S.sel = "a-x-eth-1"; renderTrace(); const tr = H.el("#trace").innerHTML;
      return {kb, tr, wraps: kb.split('<div class="pwrap">').length - 1};""")
    kb, tr = r["kb"], r["tr"]
    assert '<span class="n">6</span>' in kb and r["wraps"] == 3   # ETH, SOL y la de hora futura; las tres rotas, sin gráfica
    assert "El bot aún no da velas de ETH (la ruta llega al reiniciarlo): precio en vivo desde que abriste la web" in kb
    assert "No se pudieron leer las velas de OKX EU ni las del bot (se reintenta cada minuto)" in kb
    assert '<small>Ahora</small><b class="pos">+7,00 %</b> <b class="pos">+4,00 R</b>' in kb
    assert '<small>Al stop (sigue)</small><b>+2,15 %</b><span>daría <span class="pos">+2,75 R</span> · +1,38 USDC</span>' in kb
    assert '<small>Cobrado</small><b class="pos">+1,50 R</b><span>la mitad en 3R · el resto corre sin objetivo</span>' in kb
    assert "SL 95,00" in kb and "TP 106,00" in kb
    assert "Posición paper" in tr and '<svg viewBox="0 0 540 220"' in tr and "SL sigue 95,00" in tr and "Cobrada 3R 94,00" in tr
    assert "SL inicial 102,00" in tr and 'data-close="a-x-eth-1"' in tr and "Suelo" not in tr
    _sin_null(kb, tr)


def test_grafica_en_la_demostracion_con_velas_simuladas_coherentes():
    """En la demostración, cada operación abierta del motor simulado («6 diarias v2») y de la simulación de noticias
    lleva su gráfica con velas SIMULADAS, rotuladas como ejemplo y sin pedir nada al bot. El camino es el mismo en cada
    pintado y cuadra con lo que pasó: sin la parcial, nunca toca el stop ni llega a 3R; con la mitad cobrada, llega a 3R a
    esa hora y después no baja del stop del cerrojo."""
    r = run("""const colsOf = html => Object.fromEntries(html.split('<div class="col">').slice(1).map(c => [c.split("</h3>")[0].split("<h3>")[1], c]));
      const M = 60e3;
      S.vnow = Date.UTC(2026, 9, 28, 21, 0); S.decDemoK = null; S.demoMotor = null; S.tab = "trades"; renderAll(true);
      const kb = colsOf(H.el("#kanban").innerHTML)["En posición"]; renderAll(true); const kb2 = colsOf(H.el("#kanban").innerHTML)["En posición"];
      const plans = new Map(), by = new Map();
      for (const x of S.dec){ if (x.fase==="entrada" && x.decision==="entra" && x.datos && x.datos.plan) plans.set(x.ventana_id + "|" + x.coin, x.datos.plan);
        if (x.trade_id){ const g = by.get(x.trade_id) || {}; g[x.decision] = g[x.decision] || x; by.set(x.trade_id, g); } }
      const checks = [];
      for (const g of by.values()){ if (!g.abierta || g.cerrada) continue;
        const P = demoChartPlan(g.abierta, plans.get(g.abierta.ventana_id + "|" + g.abierta.coin) || {}, g.parcial), d = P.d;
        const B = demoPathBars(P, M, P.t0 - 60*M, S.vnow), after = B.rows.filter(x => x[0] >= P.t0 + M);
        const worst = x => d > 0 ? x[3] : x[2], best = x => d > 0 ? x[2] : x[3];
        if (!P.done) checks.push(after.every(x => d*(worst(x) - P.sl0) > 0 && d*(best(x) - P.tp1) < 0));
        else { const post = after.filter(x => x[0] >= P.act + M);
          checks.push(after.some(x => d*(best(x) - P.tp1) >= -P.entry*1e-9) && post.every(x => d*(worst(x) - P.floorPx) > 0)
            && d*(B.sl - P.floorPx) >= -1e-9 && d*(B.px - B.sl) > 0); }
        checks.push(demoPathBars(P, M, P.t0 - 60*M, S.vnow) === B);
        S.demoPaths.clear(); checks.push(JSON.stringify(demoPathBars(P, M, P.t0 - 60*M, S.vnow).rows) === JSON.stringify(B.rows)); }
      return {kb, kb2, checks, sims: S.positions.filter(p => chartPlan(p)).length,
        fetches: H.fetches.filter(u => String(u).includes("/api/velas")).length};""", live=False)
    kb = r["kb"]
    assert r["checks"] and all(r["checks"]) and r["fetches"] == 0
    assert kb.count('<div class="pwrap"><button type="button" class="ticket motor"') == kb.count("Motor «6 diarias v2»") > 0
    assert kb.count('<div class="pwrap">') == kb.count("Motor «6 diarias v2»") + r["sims"]
    assert "Ejemplo: velas simuladas de" in kb and "del bot" not in kb and kb == r["kb2"]   # el mismo en cada pintado
    _sin_null(kb)


# ---------- Comisiones en el sistema, velas de OKX EU y modo real preparado (29-10-2026) ----------
# Perfil de comisiones como lo manda la cartera (portfolio.comisiones = bot/comisiones.vista) y costes de cada operación
# (paper._costes: entrada, salidas, funding, total y sus R; en las abiertas, además las tasas y lo que costaría salir).
COM = r"""
const vistaCom = (o = {}) => ({perfil: "okx_eu_con_xperps", nombre: "OKX EU con cuenta X-Perps", elegido: "okx_eu_con_xperps", config: "okx_eu_con_xperps",
  por_defecto: "okx_eu_con_xperps", cuenta: false, spot: {creador: 0.08, tomador: 0.1}, perp: {creador: 0.02, tomador: 0.05}, cortos: true,
  texto: "contado 0,08 % creador / 0,10 % tomador; X-Perps 0,02 % / 0,05 %; cortos en X-Perps", fuente: "Ayuda oficial de OKX EU",
  regla_coste_pct: {spot: 0.24, perp: 0.14}, deslizamiento_bps: 2, aviso: "Sin cuenta X-Perps en OKX EU no hay cortos",
  perfiles: [{id: "okx_eu_con_xperps", nombre: "OKX EU con cuenta X-Perps", cortos: true}, {id: "okx_eu_sin_xperps", nombre: "OKX EU sin cuenta X-Perps", cortos: false}], ...o});
const SIN = {perfil: "okx_eu_sin_xperps", nombre: "OKX EU sin cuenta X-Perps", elegido: "okx_eu_sin_xperps", spot: {creador: 0.2, tomador: 0.35}, perp: null, cortos: false,
  regla_coste_pct: {spot: 0.74}};
const CAPS = ["halt", "cancel", "comisiones"];
"""


def test_comisiones_perfil_con_y_sin_xperps_y_su_selector():
    """La cartera dice el perfil de comisiones en uso y la web lo enseña claro: tasas de tomador (a mercado) y creador
    (orden límite), X-Perps, deslizamiento y coste de la regla del 3 % neto, y que sin X-Perps no hay cortos y el contado
    cuesta 0,35 %. El selector solo sale si el bot admite el mando («comisiones» en caps), pide confirmación, envía
    {type: "comisiones", perfil} y avisa con «orden_web». Sin X-Perps: sin cortos, también en Mando. Un bot de antes (sin
    perfil) se ve como 0,10 % por lado; la comisión real de la cuenta, como tal."""
    r = run(COM + """H.open(); reset();
      H.recv(pf({caps: CAPS, comisiones: vistaCom({nombre: "OKX EU con cuenta X-Perps <b>"})}));
      S.tab = "trades"; renderAll(true);
      const con = H.el("#comBox").innerHTML, opts = H.el("#comSel").innerHTML, selVal = H.el("#comSel").value, selHid = H.el("#comSel").hidden;
      const sel = H.el("#comSel");
      H.confirmAnswer = false; sel.value = "okx_eu_sin_xperps"; H.elFire("#comSel", "change", {target: sel});
      const nada = H.sent().length, vuelve = sel.value;
      H.confirmAnswer = true; sel.value = "okx_eu_sin_xperps"; H.elFire("#comSel", "change", {target: sel});
      const t1 = H.el("#toast").textContent, sent = H.sent().slice();
      H.recv({type: "orden_web", ts: Date.now(), area: "comisiones", accion: "perfil", id: "okx_eu_sin_xperps", ok: true, why: null});
      const t2 = H.el("#toast").textContent;
      H.recv(pf({caps: CAPS, comisiones: vistaCom(SIN)})); renderAll(true);
      const sin = H.el("#comBox").innerHTML;
      S.tab = "mando"; renderAll(true); const md = H.el("#mdBook").innerHTML;
      H.recv(pf({caps: ["halt", "cancel"], comisiones: vistaCom()})); S.tab = "trades"; renderAll(true); const hid = H.el("#comSel").hidden, noCap = H.el("#comBox").innerHTML;
      H.recv(pf({caps: CAPS})); renderAll(true); const viejo = H.el("#comBox").innerHTML, hidV = H.el("#comSel").hidden;
      H.recv(pf({caps: CAPS, comisiones: vistaCom({perfil: "cuenta", cuenta: true, elegido: "okx_eu_con_xperps", nombre: "Comisión real de tu cuenta de OKX EU (leída en solo lectura)",
        spot: {creador: 0.08, tomador: 0.1}, perp: null, cortos: false, por_que: "Sin cortos: tu cuenta no tiene X-Perps"})})); renderAll(true);
      const cuenta = H.el("#comBox").innerHTML, cselVal = H.el("#comSel").value;
      H.recv(pf({caps: CAPS, comisiones: vistaCom({...SIN, config: "okx_eu_sin_xperps",
        aviso_config: "BOT_COMISIONES=okx_eu_sin_xperp no es un perfil conocido (okx_eu_con_xperps o okx_eu_sin_xperps): se usa el más caro, «OKX EU sin cuenta X-Perps». Corrígelo <b>"})}));
      renderAll(true);
      return {con, opts, selVal, selHid, nada, vuelve, t1, sent, t2, sin, md, hid, noCap, viejo, hidV, cuenta, cselVal, errata: H.el("#comBox").innerHTML};""")
    con, sin = r["con"], r["sin"]
    assert "<b>OKX EU con cuenta X-Perps &lt;b&gt;</b>" in con and "Por defecto (con X-Perps)" in con and "Con cortos (X-Perps)" in con
    assert "Hasta el 29-10-2026 el paper cobraba 0,10 % en todo; con este perfil, el objetivo y la parcial (orden límite) pagan 0,08 %" in con
    # La horquilla de «6 diarias» en dinero, después de costes: 2,5:1 con X-Perps y 1,8:1 sin ellas (por debajo de 2:1)
    assert '<small>«6 diarias» en dinero (stop 1,5 %, 3R)</small><b class="">2,5:1 neto</b>' in con
    assert "(con X-Perps 2,5:1, sin X-Perps 1,8:1); tu regla ≥ 2:1 se mide en R de precio, antes de costes" in con
    assert '<b class="neg">1,8:1 neto</b>' in sin and "Hasta el 29-10-2026" not in sin
    for t in ("<small>Contado · a mercado (tomador)</small><b>0,10 %</b>", "<small>Contado · orden límite (creador)</small><b>0,08 %</b>",
              "<b>0,05 % / 0,02 %</b>", "<b>2 pb + ½ diferencial</b>", "<b>0,24 % · 0,14 %</b>",
              "Ojo: sin cuenta X-Perps no hay cortos y el contado cuesta 0,35 %"):
        assert t in con, t
    assert 'value="okx_eu_con_xperps"' in r["opts"] and 'value="okx_eu_sin_xperps"' in r["opts"] and 'value="cuenta"' not in r["opts"]
    assert r["selVal"] == "okx_eu_con_xperps" and r["selHid"] is False
    assert r["nada"] == 0 and r["vuelve"] == "okx_eu_con_xperps"   # sin confirmar: nada enviado y el selector vuelve
    assert r["sent"] == [{"type": "comisiones", "perfil": "okx_eu_sin_xperps"}] and "Pendiente de que lo confirme" in r["t1"]
    assert "Hecho: las comisiones del paper pasan a «OKX EU sin cuenta X-Perps»" in r["t2"]
    assert "<b>OKX EU sin cuenta X-Perps</b>" in sin and "Sin cortos</span>" in sin and "<b>0,35 %</b>" in sin and "<b>0,20 %</b>" in sin
    assert "Sin cuenta X-Perps: el motor no abre cortos" in sin and "<b>sin cortos</b>" in sin and "<b>0,74 %</b>" in sin and "Por defecto" not in sin
    assert "OKX EU sin X-Perps · <span class=\"neg\">sin cortos</span>" in r["md"]
    assert r["hid"] is True and "aún no admite cambiar el perfil desde la web" in r["noCap"]
    assert r["hidV"] is True and "0,10 % por lado (el bot aún no dice su perfil)" in r["viejo"] and "aún no publica su perfil" in r["viejo"]
    assert "Leída de tu cuenta (solo lectura)" in r["cuenta"] and "Manda la comisión real de tu cuenta de OKX" in r["cuenta"]
    assert "Sin cortos: tu cuenta no tiene X-Perps" in r["cuenta"] and r["cselVal"] == "okx_eu_con_xperps"
    assert "Solo la de tu cuenta de verdad (OKX_DEMO=0), nunca la de la demo" in r["cuenta"]
    assert ('<div class="com-warn"><b>Ojo con tu configuración:</b> BOT_COMISIONES=okx_eu_sin_xperp no es un perfil conocido'
            in r["errata"] and "Corrígelo &lt;b&gt;." in r["errata"] and "Ojo con tu configuración" not in r["con"])
    _sin_null(con, sin, r["md"], r["noCap"], r["viejo"], r["cuenta"], r["errata"])


def test_costes_de_cada_operacion_en_cartera_resultados_y_grafica():
    """Cada operación del paper lleva sus costes (entrada, salidas, funding y total en USDC y en R): en la tarjeta de la
    cartera (lo pagado y lo que costaría salir ahora y al stop), en la gráfica («Costes»: lo pagado, al stop a mercado y en
    la parcial con orden límite), en las cerradas de la cartera y de Resultados (total, R y el desglose con su perfil) y en
    los KPI de Resultados («Comisiones pagadas» y bruto frente a neto). Un bot de antes, sin totales, se suma de cada
    operación; una cerrada antes de los perfiles sale como estimada."""
    r = run(COM + FIAB + SEIS2 + GRAF + """H.open(); reset();
      H.fetchReply = url => url.startsWith("/api/velas") ? velasOk({coin: "BTC", tf: "1m", velas: BARS, hasta_datos: now - 2*M, fuente: "okx", instId: "BTC-USDC"}) : {ok: false};
      const costes = {perfil: "okx_eu_con_xperps", entrada: 0.025, salidas: 0, comisiones: 0.025, funding: 0, total: 0.025, entrada_R: 0.05, salidas_R: 0, funding_R: 0,
        total_R: 0.05, tomador: 0.1, creador: 0.08, salir_ahora: 0.0251, salir_ahora_R: 0.0502, objetivo: null};
      const pos = {...posA, costes};
      const cerr = {id: "t1", story: "a-x-eth-9", strategy: "impulso-largo", window: "Europa", coin: "ETH", side: "long", qty: 0.01, entry: 2500, exit: 2650, opened: now - 9*3600e3,
        closed: now - 3600e3, closedAt: now - 3600e3, reason: "Objetivo (3,0 R)", pnl: 1.44, pnl_pct: 5.8, R: 2.88, fees: 0.046, commissions: 0.046, funding: 0, riskUsd: 0.5, notional: 25,
        headline: "Compra ETH", instrument: "spot", bruto: 1.486,
        costes: {perfil: "okx_eu_con_xperps", entrada: 0.025, salidas: 0.0212, comisiones: 0.0462, funding: 0, total: 0.0462, total_R: 0.0924, salida_tipo: "creador"}};
      const corto = {...cerr, id: "t2", story: "a-x-sol-9", coin: "SOL", side: "short", instrument: "perp", pnl: -0.53, R: -1.06, reason: "Stop de pérdida", fees: 0.02, commissions: 0.025,
        funding: 0.005, bruto: -0.51, costes: {perfil: "okx_eu_con_xperps", entrada: 0.0125, salidas: 0.0125, comisiones: 0.025, funding: 0.005, total: 0.02, total_R: 0.04}};
      const viejo = {...cerr, id: "t3", story: "a-x-xrp-9", coin: "XRP", pnl: 0.3, R: 0.6, fees: 0.05, commissions: 0.05, funding: 0, bruto: 0.35,
        costes: {perfil: null, entrada: 0.025, salidas: 0.025, comisiones: 0.05, funding: 0, total: 0.05, total_R: 0.1, estimado: true}};
      H.recv({type: "market", prices: {BTC: 100.4}}, pf({caps: CAPS, comisiones: vistaCom(), positions: [pos], closed: [cerr, corto, viejo]}));
      S.results = {start_equity: 111.37, equity: 112.58, trades: [cerr, corto, viejo], by_strategy: [], by_window: [], by_coin: [], equity_curve: [], daily: [],
        comisiones_total: 0.12, funding_total: 0.01, costes_total: 0.11, bruto: 1.32, neto: 1.21, costes_estimados: 1, comisiones: vistaCom()};
      S.tab = "trades"; renderAll(true); await esperaVelas(); renderAll(true);
      const kb = colsOf(H.el("#kanban").innerHTML)["En posición"], cl = H.el("#closedRows").innerHTML;
      S.sel = "a-tok-btc-1"; renderTrace(); const tr = H.el("#trace").innerHTML;
      S.tab = "results"; renderAll(true); renderResults();
      const k = H.el("#resKpis").innerHTML, rows = H.el("#resRows").innerHTML, rc = H.el("#resCom").innerHTML;
      S.results = {start_equity: 111.37, equity: 112.58, trades: [cerr, corto], by_strategy: [], by_window: [], by_coin: [], equity_curve: [], daily: []};
      renderResults(); const k2 = H.el("#resKpis").innerHTML;
      return {kb, cl, tr, k, rows, rc, k2};""")
    kb, cl, tr, k, rows = r["kb"], r["cl"], r["tr"], r["k"], r["rows"]
    # salir ahora (100,4) y al stop (98), con la misma base: 0,25 × precio × (0,10 % tomador + 3 pb de deslizamiento)
    assert "Costes 0,025 USDC (0,05 R) pagados · salir ahora ≈ 0,033 · al stop ≈ 0,032" in kb
    assert "<small>Costes</small><b>0,025</b> <b>0,05 R</b><span>pagados · salir ahora ≈ 0,033 · al stop ≈ 0,032 · a la parcial ≈ 0,011 USDC</span>" in kb
    assert ("pagados (entrada 0,025) · salir ahora ≈ 0,033 · al stop ≈ 0,032 · a la parcial ≈ 0,011 USDC · a mercado (ahora y al stop) "
            "tomador 0,10 % y deslizamiento; con orden límite (parcial) creador 0,08 %") in tr
    assert "«Ahora» ya descuenta lo pagado (comisión de entrada, parciales y funding); «al stop» y «al objetivo», sin comisiones" in tr
    assert "va en «Costes» (perfil OKX EU con X-Perps)" in tr and "USDC sin comisiones ni deslizamiento" not in tr
    assert 'title="Comisiones 0,046 USDC (entrada 0,025 · salidas 0,021) (paper, perfil «OKX EU con cuenta X-Perps»)">0,046<br><span class="note">0,09 R</span>' in cl
    assert "funding cobrado 0,01 USDC" in cl and "funding +0,01" in cl   # el corto en perpetuo cobró funding
    assert "estimadas: cerrada antes de las comisiones por perfil" in cl
    for h in (cl, rows):
        assert h.count("<td class=\"r mono\" title=\"Comisiones") == 3
    assert ("<small>Comisiones pagadas</small><b>0,120</b><span>USDC de comisión en 3 operaciones cerradas (1 estimada: de antes del "
            "29-10) · +0,025 ya pagados en las abiertas · funding cobrado 0,01</span>") in k
    assert '<small>Bruto → neto</small><b><span class="pos">+1,32</span> → <span class="pos">+1,21</span></b>' in k and "los costes se llevan el 8 % del bruto" in k
    assert "Perfil en uso: OKX EU con cuenta X-Perps." in r["rc"]
    # sin totales del bot (uno de antes): se suman de cada operación
    assert "<small>Comisiones pagadas</small><b>0,071</b>" in r["k2"] and '<span class="pos">+0,98</span> → <span class="pos">+0,91</span>' in r["k2"]
    _sin_null(kb, cl, tr, k, rows, r["rc"], r["k2"])


def test_grafica_velas_de_okx_respaldo_del_bot_y_404():
    """La gráfica pide las velas de OKX EU (fuente=okx) y lo dice en la nota con el par («velas de OKX EU, BTC-USDC»). Si
    OKX falla, el bot da las suyas con fuente «bot» y su aviso, y la nota lo dice; un bot de antes (sin «fuente») da las
    suyas y la nota avisa de que aún no pide las de OKX; un 404 se tolera con el precio en vivo."""
    r = run(FIAB + SEIS2 + GRAF + """H.open(); reset();
      const rep = {BTC: {fuente: "okx", instId: "BTC-USDC"}, ETH: {fuente: "bot", pedida: "okx", instId: "ETHUSDT", aviso: "OKX EU no respondió (timeout <x>)"}, SOL: {}};
      H.fetchReply = url => { const c = /coin=(\\w+)/.exec(url)[1];
        if (c==="XRP" && H.sinVelas) return {ok: false, status: 404, text: async () => "Sin velas de XRP en la caché del bot en ese tramo (OKX EU no respondió (<y>))"};
        return rep[c] ? velasOk({coin: c, tf: "1m", velas: BARS, hasta_datos: now - 2*M, ...rep[c]}) : {ok: false, status: 404}; };
      const mk = (coin, id) => ({...posA, coin, story: "s-" + coin, id, headline: "Compra " + coin});
      H.recv({type: "market", prices: {BTC: 100.4, ETH: 100.4, SOL: 100.4, XRP: 100.4}}, pf({positions: [mk("BTC", "q1"), mk("ETH", "q2"), mk("SOL", "q3"), mk("XRP", "q4")]}));
      S.tab = "trades"; renderAll(true); await esperaVelas(); renderAll(true);
      const kb = colsOf(H.el("#kanban").innerHTML)["En posición"];
      const urls = H.fetches.filter(u => String(u).startsWith("/api/velas"));
      H.sinVelas = true; S.velas = {}; renderAll(true); await esperaVelas(); renderAll(true);
      return {kb, urls, kb2: colsOf(H.el("#kanban").innerHTML)["En posición"]};""")
    kb = r["kb"]
    assert len(r["urls"]) == 4 and all(u.endswith("&tf=1m&fuente=okx") for u in r["urls"])
    assert "Velas de OKX EU, BTC-USDC, de 3 min y precio en vivo · hora de Madrid" in kb
    assert "Velas del bot de 3 min (Binance, ETHUSDT), no de OKX EU (OKX EU no respondió (timeout &lt;x&gt;)); se reintenta cada minuto" in kb
    assert "Velas de 3 min del bot y precio en vivo · hora de Madrid (este bot aún no pide las de OKX EU: llega al reiniciarlo)" in kb
    assert "El bot aún no da velas de XRP (la ruta llega al reiniciarlo)" in kb and "<x>" not in kb
    # el bot de ahora sin velas de ese tramo (404 con su texto): no es «la ruta llega al reiniciarlo»
    assert "Ni OKX EU ni el bot tienen velas de XRP de ese tramo (Sin velas de XRP en la caché del bot" in r["kb2"]
    assert "El bot aún no da velas de XRP" not in r["kb2"] and "<y>" not in r["kb2"]
    _sin_null(kb, r["kb2"])


# Estado «cuenta_real» / GET /api/real (RealBroker.vista_real, docs/06 apartado 5) en cada modo.
REAL = r"""
const LIM = {max_usdc: 10, perdida_dia_pct: 3, max_posiciones: 4, max_entradas_dia: 6};
const vista = (o = {}) => ({type: "cuenta_real", ts: now, modo: "apagado", motivos: ["Apagado: no hay archivo de claves (M:\\BotFundamental-datos\\secretos\\okx.env)"],
  demo: false, opera: false, etiqueta: "solo lectura", listo: false, puede_abrir: false,
  por_que: "Modo real no activo: Apagado: no hay archivo de claves (M:\\BotFundamental-datos\\secretos\\okx.env)", bloqueo: null, avisos: [], parada: null, parando: false,
  halted: false, halt_until: null, aprobacion: "veto", archivos: {carpeta: "M:\\BotFundamental-datos\\secretos", claves: false, confirmado: null, parar: false},
  frase_confirmacion: "ACTIVO EL MODO REAL", cuenta: {equity: null, usdc_disponible: null, monedas: null, acctLv: null, posMode: null, ip_atada: null, permisos: null,
  saldo_at: null, day_pnl: 0, day_pnl_pct: 0, inicio: null}, comisiones: {spot: null, perp: null}, comisiones_creador: {spot: null, perp: null},
  cortos: {ok: false, por_que: "Aún no se ha leído la cuenta"}, instrumentos: [], limites: LIM, techos: {...LIM}, entradas_hoy: 0, posiciones: [], sin_stop: [],
  apartadas: [], externas: [], pendientes: [], cerradas: [], reconciliacion: null, ultimo_error: null, peticiones: 0, broker: "paper",
  resumen: "Modo real apagado: el bot opera en paper", OKX_API_KEY: "clave-secreta-123", secret: "SECRETO_FALSO", ...o});
const leida = (o = {}) => vista({modo: "solo_lectura", motivos: ["Solo lectura (ninguna orden)", "Falta arrancar el bot con BOT_MODO_REAL=1 <b>"], listo: true,
  por_que: "Modo real no activo: falta <script>x</script>", avisos: ["La clave no está atada a la IP de tu PC (y OKX la caduca tras 14 días sin uso)"],
  archivos: {carpeta: "M:\\BotFundamental-datos\\secretos", claves: true, confirmado: null, parar: false},
  cuenta: {equity: 0, usdc_disponible: 0, monedas: {BTC: 0, ETH: 0, SOL: 0, XRP: 0}, acctLv: "1", posMode: "", ip_atada: false, permisos: "read_only,trade",
    saldo_at: now - 30e3, day_pnl: 0, day_pnl_pct: 0, inicio: null},
  comisiones: {spot: 0.35, perp: null}, comisiones_creador: {spot: 0.2, perp: null}, cortos: {ok: false, por_que: "Sin cortos: tu cuenta no tiene X-Perps disponibles"},
  reconciliacion: {at: now - 60e3, cambios: ["Sin cambios <i>"], externas: []}, peticiones: 12,
  resumen: "Modo real en solo lectura: el bot opera en paper (solo lee tu cuenta de OKX)", ...o});
const posR = {story: "r-btc", coin: "BTC", side: "long", qty: 0.0001, entry: 100, sl: 98, sl_initial: 98, opened: now - 3600e3, riskUsd: 0.2, notional: 10, price: 101,
  upnl: 0.09, R: 0.45, tp1: 106, tp1_frac: 0.5, floor: 104, locked: false, instId: "BTC-USDC", instrument: "spot", real: true, estado: "abierta", sin_stop: true,
  sin_stop_desde: now - 120e3, partes: [{n: 1, sz: "0.00005", sl: "98", tp: "106", estado: "pendiente", algoId: null}, {n: 2, sz: "0.00005", sl: 98, tp: null, estado: "viva", algoId: "1"}]};
const real = (o = {}) => leida({modo: "real", motivos: ["Modo real ACTIVO: opera con tu dinero en OKX EU"], puede_abrir: true, por_que: null, broker: "real",
  resumen: "Operando en dinero real", archivos: {carpeta: "M:\\BotFundamental-datos\\secretos", claves: true, confirmado: "29-10-2026 10:15", parar: false},
  cuenta: {equity: 52.4, usdc_disponible: 44.3, monedas: {BTC: 0.0001, ETH: 0}, acctLv: "2", posMode: "net_mode", ip_atada: true, permisos: "read_only,trade",
    saldo_at: now - 20e3, day_pnl: -0.06, day_pnl_pct: -0.11, inicio: 50},
  comisiones: {spot: 0.1, perp: 0.05}, comisiones_creador: {spot: 0.08, perp: 0.02}, cortos: {ok: true, por_que: "Cortos en X-Perps: BTC, ETH, SOL, XRP"},
  posiciones: [posR, {story: "r-eth", coin: "ETH", side: "short", estado: "enviando", sin_stop: true}], sin_stop: ["BTC", "ETH"],
  pendientes: [{story: "r-sol", coin: "SOL", side: "short", deadline: now + 20e3, mode: "veto"}],
  externas: [{coin: "XRP", instId: "XRP-USD_UM_XPERP-310404", pos: "-3", avgPx: 1.3}],
  apartadas: [{story: "x", coin: "ETH", side: "long", estado: "abierta", modo: "demo", entorno: "demo", instId: "ETH-USDC", sz: "0.01"}],
  cerradas: [{coin: "BTC", side: "long", closed: now - 7200e3, reason: "Stop de pérdida <b>", pnl: -0.2, R: -1.05}],
  ultimo_error: {at: now - 300e3, donde: "saldo", texto: "ErrorOKX: 50011 Rate limit <x>"}, ...o});
const panel = () => { renderAll(true); return ["#realBand", "#realHead", "#realCuenta", "#realCtl", "#realLim", "#realPos", "#realTec"].map(s => H.el(s).innerHTML).join("\n"); };
"""


def test_cuenta_real_panel_en_sus_modos():
    """Panel «Cuenta real» con lo que manda el bot: sin la ruta (404) dice que el modo real está apagado y el bot opera en
    paper; apagado (sin okx.env) lo dice igual, sin mandos ni frase; en solo lectura enseña la cuenta (saldo, comisión
    real, cortos, IP, permisos), los avisos, los límites con su techo, los mandos y la caja de la frase; en real, el modo
    en grande, las posiciones con sus partes, la banda roja SIN STOP (y el aviso en la pestaña), pendientes con aprobar y
    vetar, las de fuera del bot en rojo, las apartadas, las cerradas y el último error. Todo escapado y sin enseñar nunca
    nada que no sea del contrato (ni un campo con una clave)."""
    r = run(REAL + """H.open(); reset();
      H.fetchReply = url => url === "/api/real" ? {ok: false, status: 404} : {ok: false};
      H.fire("click", {"data-tab": "real"}); await loadReal();
      const p404 = panel();
      H.recv(vista()); const pOff = panel(), ctlOff = H.el("#realCtl").innerHTML, badgeOff = H.el("#realBadge").hidden, bandOff = H.el("#realBand").hidden;
      S.tab = "trades"; renderAll(true); const nota = H.el("#tradesNoteTxt").innerHTML; S.tab = "real";
      H.recv(leida()); const pRO = panel(), ctlRO = H.el("#realCtl").innerHTML;
      H.recv(real()); const pReal = panel(), band = H.el("#realBand").innerHTML, bandHid = H.el("#realBand").hidden;
      renderHeader(); const badge = H.el("#realBadge").textContent, badgeHid = H.el("#realBadge").hidden;
      H.recv(real({parada: {motivo: "Parada de emergencia desde la web", cerrar: false, ms: now - 60e3, fecha: "29-10-2026 08:01"}, modo: "solo_lectura", puede_abrir: false,
        sin_stop: [], posiciones: [{...posR, sin_stop: false}], archivos: {carpeta: "M:/x", claves: true, confirmado: null, parar: true}}));
      const pPar = panel(), ctlPar = H.el("#realCtl").innerHTML;
      S.tab = "mando"; renderAll(true);
      return {p404, pOff, ctlOff, badgeOff, bandOff, nota, pRO, ctlRO, pReal, band, bandHid, badge, badgeHid, pPar, ctlPar, md: H.el("#mdAgents").innerHTML, fetches: H.fetches};""")
    p404, pOff, pRO, pReal, pPar = r["p404"], r["pOff"], r["pRO"], r["pReal"], r["pPar"]
    assert "/api/real" in r["fetches"]
    assert "Modo real apagado: el bot opera en paper" in p404 and "GET /api/real no existe" in p404
    assert '<div class="rmode apagado"><b>Apagado</b>' in pOff and "Modo real apagado: el bot opera en paper." in pOff and "Sin claves: el bot no lee nada" in pOff
    assert "copia okx.env.ejemplo a okx.env" in pOff and 'id="realFrase"' not in pOff and "data-real-acc" not in r["ctlOff"]
    # Para empezar, solo «Lectura»; y el aviso de que con tus claves el paper cobra tu comisión real (sin X-Perps, sin cortos)
    assert "basta una clave con solo el permiso «Lectura» (saldo y comisiones)" in pOff and "Lectura y Operar" not in pOff
    assert "el paper pasa a cobrar la comisión real de tu cuenta y, si no tiene X-Perps, deja de abrir cortos" in pOff
    assert "como mucho cancela o protege con su stop órdenes que haya puesto él mismo" in pOff
    assert "Con tus claves (las de tu cuenta, no las de la demo), el paper pasa a cobrar la comisión real" in pOff
    assert r["badgeOff"] is True and r["bandOff"] is True
    assert "Modo real apagado: el bot opera en paper" in r["nota"]
    # solo lectura: cuenta leída, avisos, mandos, límites y la frase
    assert '<div class="rmode solo_lectura"><b>Solo lectura</b>' in pRO and "Los tickets van al paper (BOT_BROKER=paper)" in pRO
    assert "Modo real no activo: falta &lt;script&gt;x&lt;/script&gt;" in pRO and "BOT_MODO_REAL=1 &lt;b&gt;" in pRO and "<script>" not in pRO
    assert "La clave no está atada a la IP de tu PC" in pRO and "<b>0,35 % tomador · 0,20 % creador</b>" in pRO and "<small>IP atada</small><b><span class=\"neg\">no</span>" in pRO
    assert "Sin cortos: tu cuenta no tiene X-Perps disponibles" in pRO and "<b>1 (contado)</b>" in pRO and "USDC de verdad" in pRO
    for t in ('data-real-acc="parar"', 'data-real-acc="parar_cerrar"', 'data-real-acc="detener"', 'data-real-aprob="veto" aria-pressed="true"',
              'data-real-aprob="manual"', 'id="realFrase"', 'data-real-acc="confirmar"', "escribe exactamente «ACTIVO EL MODO REAL»"):
        assert t in r["ctlRO"], t
    assert 'data-real-aprob="auto"' not in r["ctlRO"]   # la automática solo en la demo de OKX
    assert 'data-real-lim="max_usdc" min="0" max="10"' in pRO and "Entradas hoy: 0 de 6" in pRO and "Sin cambios &lt;i&gt;" in pRO
    # real: modo en grande, posiciones, SIN STOP, pendientes, externas, apartadas, cerradas y error
    assert '<div class="rmode real"><b>Dinero real</b>' in pReal and "Operando en dinero real." in pReal and "Los tickets van al modo real" in pReal
    assert r["bandHid"] is False and "¡SIN STOP EN OKX: BTC, ETH!" in r["band"] and r["badgeHid"] is False and r["badge"] == "SIN STOP"
    assert '<tr class="sinstop"><td><b>BTC</b> <span class="pill bad">SIN STOP desde' in pReal and "Enviando a OKX o en duda" in pReal
    assert "1: pendiente 0.00005 · stop 98,00 · obj. 106,00<br>2: viva 0.00005 · stop 98,00" in pReal and 'data-real-close="r-btc"' in pReal
    assert 'data-real-approve="r-sol"' in pReal and 'data-real-reject="r-sol"' in pReal and "sale sola en" in pReal
    assert '<tr class="externa"><td><b>XRP</b></td><td class="mono">XRP-USD_UM_XPERP-310404</td>' in pReal and "<td>demo</td>" in pReal
    assert "Stop de pérdida &lt;b&gt;" in pReal and "-1,05 R" in pReal and "50011 Rate limit &lt;x&gt;" in pReal
    assert "<b>0,10 % tomador · 0,08 % creador</b>" in pReal and "<b>52,40 USDC</b>" in pReal and "confirmado el 29-10-2026 10:15" in pReal
    # parada activa: se dice, «parar» ya no se ofrece y «parar y cerrar» sí; la frase la quita
    assert "Parada de emergencia activa: Parada de emergencia desde la web (29-10-2026 08:01)" in pPar and "sin cerrar posiciones" in pPar
    assert 'data-real-acc="parar"' not in r["ctlPar"] and 'data-real-acc="parar_cerrar"' in r["ctlPar"] and "Quitar la parada" in r["ctlPar"]
    assert '<small>PARAR_REAL</small><b><span class="neg">existe</span>' in pPar
    assert "Cuenta real" in r["md"]
    for h in (p404, pOff, pRO, pReal, pPar):
        assert "clave-secreta-123" not in h and "SECRETO_FALSO" not in h and "OKX_API_KEY" not in h
    _sin_null(p404, pOff, pRO, pReal, pPar, r["nota"], r["md"])


def test_cuenta_real_mandos_seguros():
    """Mandos del panel: parada (cerrar=false) y parada con cierre (cerrar=true de JSON), cada una con su confirmación;
    límites solo hacia abajo (por encima del techo o no válidos no se envía nada); la frase tiene que ser la exacta y se
    confirma aparte; aprobación veto o a mano (la automática solo en la demo de OKX); aprobar, vetar y cerrar una de
    las suyas. La respuesta «orden_web» (area real) se avisa, también cuando el bot la rechaza por el origen. Sin
    conexión no se envía nada; un aviso «real_aviso» SIN STOP sale en rojo en el cable y lleva a la pestaña."""
    r = run(REAL + """H.open(); reset();
      H.recv(real()); H.fire("click", {"data-tab": "real"}); renderAll(true);
      H.confirmAnswer = false; H.fire("click", {"data-real-acc": "parar"}); const n0 = H.sent().length;
      H.confirmAnswer = true; H.fire("click", {"data-real-acc": "parar"}); H.fire("click", {"data-real-acc": "parar_cerrar"});
      const tPar = H.el("#toast").textContent;
      H.recv({type: "orden_web", ts: Date.now(), area: "real", accion: "parar", id: null, ok: true, why: "La parada ya estaba pedida"});
      const tOk = H.el("#toast").textContent;
      S.realForm.lim = {max_usdc: "25"}; H.fire("click", {"data-real-acc": "limites"}); const tTecho = H.el("#toast").textContent, n1 = H.sent().length;
      S.realForm.lim = {max_posiciones: "1.5"}; H.fire("click", {"data-real-acc": "limites"}); const tMal = H.el("#toast").textContent, n2 = H.sent().length;
      S.realForm.lim = {max_usdc: "5,5", max_entradas_dia: "3"}; H.fire("click", {"data-real-acc": "limites"}); const n3 = H.sent().length;
      S.realForm.frase = "activo el modo"; H.fire("click", {"data-real-acc": "confirmar"}); const tFr = H.el("#toast").textContent, n4 = H.sent().length;
      S.realForm.frase = "  activo   el modo REAL "; H.fire("click", {"data-real-acc": "confirmar"}); const frase = S.realForm.frase;
      H.fire("click", {"data-real-aprob": "auto"}); const tAuto = H.el("#toast").textContent, n5 = H.sent().length;
      H.fire("click", {"data-real-aprob": "manual"}); H.fire("click", {"data-real-approve": "r-sol"}); H.fire("click", {"data-real-reject": "r-sol"});
      H.fire("click", {"data-real-close": "r-btc"}); H.fire("click", {"data-real-acc": "detener"});
      H.recv({type: "orden_web", ts: Date.now(), area: "real", accion: "detener", id: null, ok: false, why: "Solo desde la propia web del bot (cabecera Origin)"});
      const tOrig = H.el("#toast").textContent;
      H.recv(ev("real_aviso", "real", {aviso: {sin_stop: true}, real: {modo: "real", demo: false}}, {agent: "real", title: "BTC sin stop en OKX <b>"}));
      const tSin = H.el("#toast").textContent; S.tab = "live"; renderAll(true); const feed = H.el("#feed").innerHTML;
      const sent = H.sent().slice(); H.ws().readyState = 3; H.fire("click", {"data-real-acc": "parar"}); const tSinCon = H.el("#toast").textContent;
      return {n0, tPar, tOk, tTecho, n1, tMal, n2, n3, tFr, n4, frase, tAuto, n5, tOrig, tSin, feed, sent, after: H.sent().length, tSinCon};""")
    assert r["n0"] == 0 and "Pendiente de que la confirme" in r["tPar"] and "Hecho (modo real): parada de emergencia hecha. La parada ya estaba pedida" in r["tOk"]
    assert "no se puede subir por encima de 10,00 desde la web" in r["tTecho"] and r["n1"] == 2
    assert "Posiciones abiertas: valor no válido" in r["tMal"] and r["n2"] == 2 and r["n3"] == 3
    assert "Escribe exactamente «ACTIVO EL MODO REAL»" in r["tFr"] and r["n4"] == 3 and r["frase"] == ""
    assert "La aprobación automática solo vale en la demo de OKX" in r["tAuto"] and r["n5"] == 4
    assert "El bot no lo ha hecho (modo real): Solo desde la propia web del bot (cabecera Origin)" in r["tOrig"]
    assert "¡SIN STOP EN OKX!" in r["tSin"] and "Cuenta real" in r["tSin"]
    assert 'data-tabgo="real"' in r["feed"] and "SIN STOP EN OKX" in r["feed"] and "BTC sin stop en OKX &lt;b&gt;" in r["feed"] and 'class="pill bad"' in r["feed"]
    assert r["sent"] == [
        {"type": "real", "accion": "parar", "cerrar": False}, {"type": "real", "accion": "parar", "cerrar": True},
        {"type": "real", "accion": "limites", "limites": {"max_usdc": 5.5, "max_entradas_dia": 3}},
        {"type": "real", "accion": "confirmar", "frase": "activo   el modo REAL"},
        {"type": "real", "accion": "aprobacion", "mode": "manual"},
        {"type": "decision", "story": "r-sol", "ok": True}, {"type": "decision", "story": "r-sol", "ok": False},
        {"type": "close", "story": "r-btc"}, {"type": "real", "accion": "detener"}]
    assert r["after"] == len(r["sent"]) and "Sin conexión con el bot" in r["tSinCon"]


def test_cuenta_real_y_comisiones_en_la_demostracion():
    """En la demostración el panel «Cuenta real» es un EJEMPLO rotulado: arranca en «Apagado» (lo de verdad), se puede
    ver cómo quedaría en cada modo y sus mandos no envían nada. El perfil de comisiones de ejemplo cambia las operaciones
    simuladas: sin X-Perps, el motor simulado no abre cortos («sin cortos») y el resto de la página sigue diciendo que el
    modo real está apagado."""
    r = run("""const colsOf = html => Object.fromEntries(html.split('<div class="col">').slice(1).map(c => [c.split("</h3>")[0].split("<h3>")[1], c]));
      H.fire("click", {"data-tab": "real"}); renderAll(true);
      const off = H.el("#realHead").innerHTML, note = H.el("#realDemoNote").innerHTML, chips = H.el("#realDemoCtl").innerHTML, noteHid = H.el("#realDemoNote").hidden;
      const modos = {};
      for (const m of ["solo_lectura", "demo", "real"]){ H.fire("click", {"data-real-demo": m}); renderAll(true);
        modos[m] = ["#realHead", "#realCuenta", "#realCtl", "#realLim", "#realPos", "#realTec"].map(s => H.el(s).innerHTML).join("\\n"); }
      H.fire("click", {"data-real-acc": "parar"}); const t1 = H.el("#toast").textContent;
      H.fire("click", {"data-real-approve": "a-eu-eth-ej"}); const t2 = H.el("#toast").textContent;
      S.tab = "mando"; renderAll(true); const md = H.el("#mdBook").innerHTML;
      const sel = H.el("#comSel"); sel.value = "okx_eu_sin_xperps"; H.elFire("#comSel", "change", {target: sel});
      S.tab = "trades"; renderAll(true); const com = H.el("#comBox").innerHTML, t3 = H.el("#toast").textContent;
      demoDecRefresh(); const sinC = S.dec.filter(x => x.motivo_codigo==="sin_cortos").length, cortos = S.dec.filter(x => x.decision==="abierta" && x.lado==="corto").length;
      return {off, note, chips, noteHid, modos, t1, t2, md, com, t3, sinC, cortos, sockets: H.sockets.length, kb: colsOf(H.el("#kanban").innerHTML)["En posición"]};""", live=False)
    assert r["noteHid"] is False and "EJEMPLO INVENTADO" in r["note"] and '<div class="rmode apagado"><b>Apagado</b><span class="pill warn">Ejemplo</span>' in r["off"]
    assert 'data-real-demo="real"' in r["chips"] and "Modo real apagado: el bot opera en paper." in r["off"]
    assert '<div class="rmode solo_lectura">' in r["modos"]["solo_lectura"] and 'id="realFrase"' in r["modos"]["solo_lectura"]
    assert '<div class="rmode demo">' in r["modos"]["demo"] and 'data-real-aprob="auto"' in r["modos"]["demo"]
    assert '<div class="rmode real">' in r["modos"]["real"] and "BTC-USDC" in r["modos"]["real"] and 'class="externa"' in r["modos"]["real"]
    assert "Ejemplo: en la demostración no se envía nada al bot." in r["t1"] and "Ejemplo: en la demostración" in r["t2"] and r["sockets"] == 0
    assert "Modo real apagado: el bot opera en paper" in r["md"]   # fuera del panel, lo de verdad
    assert "<b>OKX EU sin cuenta X-Perps</b>" in r["com"] and "Ejemplo" in r["com"] and "operaciones simuladas" in r["t3"]
    assert r["sinC"] > 0 and r["cortos"] == 0
    for h in (r["off"], *r["modos"].values(), r["md"], r["com"], r["kb"]):
        _sin_null(h)


def test_backtest_dice_con_que_comisiones_esta_calculado():
    """Cada backtest resumido dice con qué comisiones está calculado (resumen_backtest.costes) y que sin cuenta X-Perps
    las cifras serían peores; sin ese dato (un resumen de antes), el modelo de siempre: 0,10 % por lado y 2 pb."""
    costes = {"perfil": "okx_eu_con_xperps", "comision_pct": 0.1, "deslizamiento_bps": 2.0, "coste_rt_pct": 0.24,
              "texto": "Calculado con 0,10 % de comisión por lado (también en los perpetuos) y 2 pb de deslizamiento por lado: "
                       "el perfil «OKX EU con cuenta X-Perps» (tomador del contado) <b>",
              "aviso": "Sin cuenta X-Perps el contado cuesta 0,35 % por lado (tomador) y no hay cortos: con ese perfil estas cifras "
                       "serían peores", "perfil_en_uso": "okx_eu_con_xperps"}
    imp, seis = {**IMP_BT, "costes": costes}, {**SEIS_BT, "costes": costes}
    r = run("const now = Date.now();" + FIAB + SEIS2 + f"""S.btImp = {{impulso: {json.dumps(imp)}, lab: null, seis: {json.dumps(seis)}, seis2: {json.dumps(SEIS2_BT)}}};
      S.openings = opn(SEIS2_P); S.tab = "bt"; S.btSig = null; renderAll(true);
      return {{s2: H.el("#btSeis2Top").innerHTML, s1: H.el("#btSeisAlt").innerHTML, e2: H.el("#btLive").innerHTML}};""", live=False)
    for h in (r["s1"], r["e2"]):
        assert ("<b>Comisiones del backtest:</b> Calculado con 0,10 % de comisión por lado (también en los perpetuos) y 2 pb de "
                "deslizamiento por lado: el perfil «OKX EU con cuenta X-Perps» (tomador del contado) &lt;b&gt;. Sin cuenta X-Perps el "
                "contado cuesta 0,35 % por lado (tomador) y no hay cortos: con ese perfil estas cifras serían peores.") in h
    assert ("<b>Comisiones del backtest:</b> 0,10 % por lado y 2 pb de deslizamiento, que no es menos que lo de OKX EU con cuenta "
            "X-Perps. Sin cuenta X-Perps no hay cortos y el contado cuesta 0,35 % a mercado: estas cifras serían peores.") in r["s2"]
    _sin_null(r["s1"], r["s2"], r["e2"])


# ---------------------------------------------------------------- Ranking de estrategias (bot/ranking.py, docs/04 2.20)
# Filas comunes con la forma de GET /api/ranking y del mensaje «ranking»; D es la que está en marcha y E pierde.
RK = r"""
const fila = (id, o = {}) => ({id, nombre: "Estrategia " + id, familia: "fam " + id, origen: "estudio", estudio: "est", regla: "Regla de " + id,
  cumple_reglas: true, perfil_costes: "okx_eu_con_xperps", perfil_texto: "OKX EU con X-Perps: contado 0,10 / 0,08 %", fecha: "2026-10-29",
  archivo: id + "_resumen.json", dentro: {n: 300, R: 0.05, gana_pct: 40}, fuera: {n: 120, R: 0.2, gana_pct: 45, p: 0.04, jornadas: 306},
  op_dia: 1, pct_dia: 0.1, R_dia: 0.2, caida_max_pct: 5,
  robustez: {sin_5_mayores: 0.05, dentro_positivo: true, corregida: {metodo: "Holm", valor: 0.03, pasa: true}, mitades: [0.1, 0.3]},
  en_vivo: null, estado: "backtest", fragil: false, fragil_motivos: [], poca_muestra: false, nueva: false, posicion: null,
  posicion_antes: null, en_marcha: false, ...o});
const rk = (filas, o = {}) => ({ts: now, n_total: filas.length + 3, n_ranking: filas.filter(f => f.posicion).length, filas, en_marcha: null,
  fuentes: {estandar: {archivos: 3, filas: 5, ts: now - 60000, errores: []}, lab_vivo: {estado: "activo", filas: 1, ts: now - 30000},
            paper: {operaciones: 2, filas: 1, ts: now}, vigilancia: {estrategias: 3, ts: now, estado: "ok"}},
  nota: "Ordenado por beneficio fuera de muestra con costes.", criterio: {orden: "pct_dia de mayor a menor", entra: "fuera.R ≥ 0"},
  origenes: [{id: "estudio", nombre: "Réplicas del motor", n: 5, n_ranking: 3}, {id: "curso", nombre: "Técnicas del curso", n: 1, n_ranking: 1}], ...o});
const A = fila("A", {posicion: 1, pct_dia: 0.3, op_dia: 2, fuera: {n: 400, R: 0.3, gana_pct: 41}, nueva: true});
const B = fila("B", {posicion: 2, nombre: "Curso <b>negrita</b>", origen: "curso", pct_dia: 0.2, op_dia: 1.5, fuera: {n: 25, R: 0.27, gana_pct: 52},
  dentro: {n: 40, R: -0.1, gana_pct: 30}, fragil: true, fragil_motivos: ["gana fuera de muestra pero pierde dentro"], poca_muestra: true,
  robustez: {sin_5_mayores: -0.2, dentro_positivo: false, corregida: {metodo: "q", valor: 0.4, pasa: false}, mitades: [0.5, -0.1]}, posicion_antes: 1});
const C = fila("C", {posicion: 3, pct_dia: 0.1, op_dia: 3, fuera: {n: 900, R: 0.07, gana_pct: 30}, cumple_reglas: false});
const D = fila("D", {posicion: 4, nombre: "Ruptura V3", pct_dia: 0.05, op_dia: 0.2, fuera: {n: 60, R: 0.5, gana_pct: 50}, estado: "en_paper", en_marcha: true,
  en_vivo: {fuente: "paper", virtual: false, n: 3, R: 0.4, gana_pct: 66.7, neto_usdc: 1.23, desde: now - 864e5, abiertas: 1}});
const E = fila("E", {posicion: null, nombre: "Pierde", pct_dia: -0.2, fuera: {n: 1000, R: -0.1, gana_pct: 30}});
const EM = (f, o = {}) => ({id: f.id, preset: "ruptura3", nombre: f.nombre, desde: now - 864e5, pausada: false, fuente: "motor", posicion: f.posicion,
  n_ranking: 4, en_ranking: !!f.posicion, texto: f.posicion ? `Puesto ${f.posicion} de 4 en el ranking` : "Fuera del ranking: pierde fuera de muestra (−0,075 R por operación)",
  fila: f, ...o});
const RES = rk([A, B, C, D, E], {en_marcha: EM(D)});
const rkReply = data => url => url.startsWith("/api/ranking") ? {ok: true, status: 200, json: async () => data} : {ok: false, status: 404};
const rkHtml = () => ["#rkLive", "#rkRows", "#rkHead", "#rkFilters", "#rkFoot", "#rkSub"].map(i => H.el(i).innerHTML).join("\n");
const visibles = () => ["A", "B", "C", "D"].filter(i => H.el("#rkRows").innerHTML.includes(`data-rk-sel="${i}"`)).join("");
"""


def test_ranking_pide_la_api_ordena_por_beneficio_y_marca_la_en_marcha():
    """Al conectar y al abrir la pestaña se pide GET /api/ranking; las filas salen en su puesto (las que pierden, nunca),
    con sus chips de robustez, la que está en marcha marcada en la tabla y aparte con su puesto, todo escapado; pulsar
    una columna ordena (y otra vez, al revés) y pulsar una estrategia enseña su regla y sus cifras. En Mando, las 5
    primeras y la que está en marcha. La web no envía nada al bot."""
    r = run(RK + """H.fetchReply = rkReply(RES);
      H.open(); reset(); await loadApi(); const f0 = H.fetches.slice();
      H.fetches.length = 0; H.fire("click", {"data-tab": "ranking"}); await loadRanking(); renderAll(true);
      const f1 = H.fetches.slice(), h0 = rkHtml(), rows0 = H.el("#rkRows").innerHTML;
      const pos = h => ["A", "B", "C", "D"].map(i => h.indexOf(`data-rk-sel="${i}"`));
      H.fire("click", {"data-rk-sort": "R"}); renderAll(true); const rDesc = pos(H.el("#rkRows").innerHTML), hR = H.el("#rkHead").innerHTML;
      H.fire("click", {"data-rk-sort": "R"}); renderAll(true); const rAsc = pos(H.el("#rkRows").innerHTML);
      H.fire("click", {"data-rk-sort": "pos"}); renderAll(true);
      H.fire("click", {"data-rk-sel": "A"}); renderAll(true); const det = H.el("#rkRows").innerHTML;
      H.fire("click", {"data-rk-sel": "A"}); renderAll(true); const cerrado = H.el("#rkRows").innerHTML;
      S.tab = "mando"; renderAll(true);
      return {f0, f1, h0, rows0, p0: pos(rows0), rDesc, rAsc, hR, det, cerrado, live: H.el("#rkLive").innerHTML, foot: H.el("#rkFoot").innerHTML,
              sub: H.el("#rkSub").innerHTML, md: H.el("#mdRk").innerHTML, badge: [H.el("#rkBadge").hidden, String(H.el("#rkBadge").textContent)], sent: H.sent()};""")
    # al conectar (en otra pestaña), las primeras, que bastan para Mando; al abrir la pestaña, la tabla entera
    assert "/api/ranking?limite=50" in r["f0"] and "/api/ranking?limite=5000" not in r["f0"] and "/api/lab_vivo" in r["f0"]
    assert "/api/ranking?limite=5000" in r["f1"]
    p0, rows = r["p0"], r["rows0"]
    assert all(x >= 0 for x in p0) and p0 == sorted(p0)   # en su puesto: % de la cuenta al día de mayor a menor
    assert 'data-rk-sel="E"' not in rows and "Pierde" not in rows   # la que pierde fuera de muestra no entra
    assert p0[2] < rows.index('<tr class="rk-live">') < p0[3] and "en marcha</span>" in rows
    assert "Curso &lt;b&gt;negrita&lt;/b&gt;" in rows and "<b>negrita</b>" not in rows
    assert 'title="gana fuera de muestra pero pierde dentro">frágil</span>' in rows and ">poca muestra</span>" in rows
    assert ">sin 5 mayores -0,20 R</span>" in rows and ">dentro ✗</span>" in rows and ">corregida q 0,40 ✗</span>" in rows
    assert ">mitades +0,50 / -0,10</span>" in rows and ">corregida Holm 0,03 ✓</span>" in rows and ">dentro ✓</span>" in rows
    assert "+0,300 %" in rows and "+0,30 R" in rows and "400 / 300" in rows and '<span class="pill info">nueva</span>' in rows
    assert 'title="Antes, puesto 1">▼1</span>' in rows and "✗ no</span>" in rows and "Descartada por ti" not in rows
    assert r["rDesc"][3] < r["rDesc"][0] < r["rDesc"][1] < r["rDesc"][2] and r["rAsc"][2] < r["rAsc"][1] < r["rAsc"][0] < r["rAsc"][3]
    assert 'aria-sort="descending"' in r["hR"] and "R/op. ▼" in r["hR"]
    assert "Regla de A" in r["det"] and 'class="rk-det"' in r["det"] and "OKX EU con X-Perps" in r["det"] and "A_resumen.json" in r["det"]
    assert 'class="rk-det"' not in r["cerrado"]
    assert "Ruptura V3" in r["live"] and "Puesto 4 de 4" in r["live"] and "En paper" in r["live"]
    assert "3 operaciones cerradas en la cartera paper" in r["live"] and "+1,23 USDC ficticios" in r["live"] and "1 abierta" in r["live"]
    assert "4 de 8 estrategias medidas no pierden fuera de muestra con costes" in r["sub"]
    assert "Estudios: 5 filas · 3 archivos" in r["foot"] and "Cómo se ordena y qué entra" in r["foot"]
    md = r["md"]
    assert md.count('data-tabgo="ranking"') == 5 and "1. Estrategia A" in md and "4. Ruptura V3" in md
    assert "Ruptura V3 · puesto 4 de 4" in md and ">+0,300 %</b>" in md
    assert r["badge"] == [False, "1"] and r["sent"] == []
    _sin_null(r["h0"], r["det"], r["live"], md)


def test_ranking_filtros_origen_reglas_operaciones_y_fragiles():
    """Los filtros van en el navegador (sin volver a pedir nada): origen (con los nombres del bot), solo las que cumplen
    tus reglas, mínimo de operaciones al día y ocultar las frágiles; «Quitar filtros» lo deja como estaba. Con un filtro
    que no deja ninguna, se dice; la que está en marcha se sigue viendo aparte."""
    r = run(RK + """H.fetchReply = rkReply(RES); H.open(); reset(); await loadApi();
      S.tab = "ranking"; renderAll(true);
      const ch = (k, v) => { const tg = H.target({"data-rk-f": k}); tg.value = v; H.fire("change", {}, {target: tg}); renderAll(true); return visibles(); };
      const out = {todas: visibles(), filtros: H.el("#rkFilters").innerHTML}, n0 = H.fetches.length;
      out.curso = ch("origen", "curso"); out.sub = H.el("#rkSub").innerHTML; ch("origen", "");
      H.fire("click", {"data-rk-t": "cumple"}); renderAll(true); out.cumple = visibles(); H.fire("click", {"data-rk-t": "cumple"});
      out.minop = ch("min_op", "0.25"); out.minop2 = ch("min_op", "2"); out.raro = ch("min_op", "7");
      H.fire("click", {"data-rk-t": "fragiles"}); renderAll(true); out.sinFragiles = visibles(); out.filtros2 = H.el("#rkFilters").innerHTML;
      H.fire("click", {"data-rk-reset": "1"}); renderAll(true); out.reset = visibles();
      ch("origen", "nadie"); out.vacio = H.el("#rkRows").innerHTML; out.live = H.el("#rkLive").innerHTML; out.mas = H.fetches.length - n0;
      return out;""")
    assert r["todas"] == "ABCD" and r["curso"] == "B" and "con tus filtros se ven 1" in r["sub"]
    assert ">Réplicas del motor (3)</option>" in r["filtros"] and ">Técnicas del curso (1)</option>" in r["filtros"]
    assert r["cumple"] == "ABD" and r["minop"] == "ABC" and r["minop2"] == "AC" and r["raro"] == "ABCD" and r["sinFragiles"] == "ACD"
    assert 'data-rk-t="fragiles" aria-pressed="true"' in r["filtros2"] and "Quitar filtros" in r["filtros2"] and r["reset"] == "ABCD"
    assert "Ninguna estrategia cumple estos filtros." in r["vacio"] and "Ruptura V3" in r["live"] and r["mas"] == 0
    _sin_null(*[v for v in r.values() if isinstance(v, str)])


def test_ranking_sin_ruta_404_por_el_bus_la_en_marcha_fuera_y_campos_rotos():
    """Un bot sin GET /api/ranking (404) no rompe nada y lo dice (también en Mando). El mensaje «ranking» del bus trae
    las 20 primeras y se enseñan (avisando de que el resto llega por la API); uno más viejo no lo pisa. La estrategia en
    marcha que pierde sale aparte, «fuera del ranking: pierde», y nunca en la tabla. Filas y campos rotos no rompen nada."""
    r = run(RK + """H.fetchReply = url => ({ok: false, status: 404});
      H.open(); reset(); await loadApi(); S.tab = "ranking"; renderAll(true);
      const sinRuta = rkHtml(), api = S.rkApi; S.tab = "mando"; renderAll(true); const md0 = H.el("#mdRk").innerHTML;
      const top = Array.from({length: 20}, (_, i) => fila("T" + i, {posicion: i + 1, pct_dia: 0.5 - i*0.01}));
      const S2 = fila("seis2", {nombre: "6 diarias v2", posicion: null, pct_dia: -0.177, op_dia: 4.72, fuera: {n: 1464, R: -0.075, gana_pct: 38.7},
        dentro: {n: 2389, R: -0.0569, gana_pct: 37.4}, estado: "en_paper", en_marcha: true});
      H.recv({type: "ranking", ts: now, n_total: 830, n_ranking: 400, filas: top, nota: "Nota del bus", fuentes: {estandar: {ts: now, filas: 700, estado: null}},
              en_marcha: EM(S2, {posicion: null, n_ranking: 400, en_ranking: false, preset: "seis2"})});
      S.tab = "ranking"; renderAll(true); const bus = rkHtml();
      S.tab = "mando"; renderAll(true); const md1 = H.el("#mdRk").innerHTML;
      H.recv({type: "ranking", ts: now - 5000, n_total: 1, n_ranking: 1, filas: [fila("VIEJA", {posicion: 1})]}); const sigue = S.rkBus.n_total;
      H.recv({type: "ranking"}, {type: "ranking", ts: now + 10, n_total: "x", n_ranking: null, en_marcha: 5, fuentes: {estandar: null, x: "texto", y: 5, z: [1]},
        criterio: 7, origenes: [null, {id: 5}], nota: {a: 1},
        filas: [null, 7, "x", {id: "x"}, {id: "y", fuera: {R: "x"}}, {id: "z", fuera: {n: 0, R: 0.5}, pct_dia: 0.1},
                {id: "w", nombre: null, fuera: {n: 10, R: 0.1, gana_pct: "x", ic95: [1]}, dentro: 5, pct_dia: "x", op_dia: null, robustez: "x", en_vivo: 5,
                 fecha: "ayer", fragil: true, fragil_motivos: [null, 3], posicion_antes: "x", estado: 9, cumple_reglas: "si", perfil_costes: {perfil: 5},
                 perfiles: {p: 5}, entra_desde: "x", caida_max_pct: NaN}]});
      S.tab = "ranking"; renderAll(true); const rotas = rkHtml();
      H.fire("click", {"data-rk-sel": "w"}); renderAll(true); const det = H.el("#rkRows").innerHTML;
      S.tab = "mando"; renderAll(true);
      return {api, sinRuta, md0, bus, md1, sigue, rotas, det, md2: H.el("#mdRk").innerHTML};""")
    assert r["api"] == "sin_ruta" and "aún no tiene el ranking (GET /api/ranking no existe" in r["sinRuta"]
    assert "Sin ranking" in r["md0"] and "aún no tiene GET /api/ranking" in r["md0"]
    bus = r["bus"]
    assert 'data-rk-sel="T0"' in bus and 'data-rk-sel="T19"' in bus and "Llegan las 20 primeras por el WebSocket" in bus
    assert ">Fuera del ranking: pierde</span>" in bus and "Fuera del ranking: pierde fuera de muestra (−0,075 R por operación)." in bus
    assert "6 diarias v2" in bus and 'data-rk-sel="seis2"' not in bus and "400 de 830 estrategias" in bus and "Nota del bus" in bus
    assert r["md1"].count('data-tabgo="ranking"') == 6 and "6 diarias v2 · fuera del ranking: pierde" in r["md1"] and "1. Estrategia T0" in r["md1"]
    assert r["sigue"] == 830
    assert 'data-rk-sel="w"' in r["rotas"] and all(f'data-rk-sel="{i}"' not in r["rotas"] for i in ("x", "y", "z"))
    assert "el ranking la marca como frágil" in r["det"]
    _sin_null(r["sinRuta"], r["md0"], bus, r["md1"], r["rotas"], r["det"], r["md2"])


def test_ranking_correccion_costes_etag_y_en_marcha_sin_datos():
    """El chip de la corrección lleva el tipo corto del bot y el método largo en el title; sin corrección siendo una de
    varias configuraciones, «sin corregir (1 de N)». Cada fila dice con qué costes se midió y el pie avisa de que no hay
    un perfil común. En Mando, «frágil» y «descartada por ti». La tabla entera se vuelve a pedir con su ETag y un 304
    deja la que hay. Con «Ocultar frágiles» y todas frágiles, se dice. Sin datos del ranking, la estrategia en marcha
    se nombra igual (por «openings»), aquí y en Mando."""
    r = run(RK + """
      const BT = "backtest con la gestión del paper: 0,10 % por lado y 2 pb (también en los perpetuos), con su funding";
      const X = fila("X", {posicion: 1, pct_dia: 0.15, origen: "salidas", probadas: 378, perfil_costes: "backtest", perfil_corto: "costes del backtest",
        perfil_texto: BT, fragil: true, fragil_motivos: ["sin corrección por número de pruebas: una de 378 configuraciones de su estudio"],
        robustez: {sin_5_mayores: null, dentro_positivo: true, mitades: null,
                   corregida: {tipo: "sin_p", metodo: "sin p ni corrección: una de 378 salidas de una rejilla", valor: null, pasa: false}}});
      const Y = fila("Y", {posicion: 2, pct_dia: 0.12, nombre: "E2 · impulso (variante de 7 días)", estado: "descartada_por_el_usuario", perfil_costes: "backtest",
        perfil_texto: BT, fragil: true, fragil_motivos: ["la salida de 7 días se eligió después de ver los datos: no está validada"],
        robustez: {corregida: {tipo: "holm", metodo: "Holm entre las 23 configuraciones del estudio (p frente a la base emparejada)", valor: 1, pasa: false}}});
      const Z = fila("Z", {posicion: 3, pct_dia: 0.1, origen: "laboratorio_2000", probadas: 2000, perfil_costes: "laboratorio", perfil_texto: null,
        fragil: true, robustez: {corregida: null}});
      const W = fila("W", {posicion: 4, pct_dia: 0.05, robustez: {corregida: {metodo: "Benjamini-Hochberg (q) entre las 2.000 del laboratorio", valor: 1, pasa: false}},
        fragil: true});
      const DATA = rk([X, Y, Z, W], {n_total: 4, n_ranking: 4, perfiles_costes: [{id: "backtest", texto: BT, n_ranking: 3},
        {id: "laboratorio", texto: "laboratorio de 2.000: 5-20 pb más en los stops y sin funding", n_ranking: 1}]});
      const calls = [];
      window.fetch = async (url, op) => { const inm = op && op.headers ? op.headers["If-None-Match"] || null : null; calls.push([url, inm]);
        if (!url.startsWith("/api/ranking")) return {ok: false, status: 404};
        const hd = {get: k => k === "ETag" ? '"v1"' : null};
        return inm === '"v1"' ? {ok: false, status: 304, headers: hd} : {ok: true, status: 200, headers: hd, json: async () => DATA}; };
      H.open(); reset(); await loadApi(); const tras1 = S.rk;
      S.tab = "ranking"; await loadRanking(); renderAll(true);
      const out = {c304: calls.filter(c => c[0].startsWith("/api/ranking")), mismo: S.rk === tras1, api: S.rkApi, rows: H.el("#rkRows").innerHTML,
                   foot: H.el("#rkFoot").innerHTML};
      H.recv({type: "ranking", ts: now - 1000, n_total: 1, n_ranking: 1, filas: [fila("BUS", {posicion: 1})]}); renderAll(true);
      out.siguenTodas = H.el("#rkRows").innerHTML.includes('data-rk-sel="W"');
      H.fire("click", {"data-rk-t": "fragiles"}); renderAll(true); out.vacio = H.el("#rkRows").innerHTML;
      S.tab = "mando"; renderAll(true); out.md = H.el("#mdRk").innerHTML;
      return out;""")
    llamadas = r["c304"]
    assert [c[1] for c in llamadas] == [None, '"v1"'] and llamadas[1][0] == "/api/ranking?limite=5000"
    assert r["mismo"] is True and r["api"] == "ok" and r["siguenTodas"] is True   # el 304 deja la tabla entera que había
    rows = r["rows"]
    assert 'title="sin p ni corrección: una de 378 salidas de una rejilla">sin corregir (1 de 378)</span>' in rows
    assert 'title="Holm entre las 23 configuraciones del estudio (p frente a la base emparejada)">corregida Holm 1,00 ✗</span>' in rows
    assert ">sin corregir (1 de 2000)</span>" in rows and ">corregida q 1,00 ✗</span>" in rows   # de un bot sin «tipo»
    assert "Holm entre las 23 configuraciones del e…" not in rows and "fam X · Salidas, stops y objetivos · costes del backtest" in rows
    assert 'title="Costes: ' + "backtest con la gestión del paper" in rows and "costes del laboratorio (sin funding)" in rows
    assert "No hay un perfil de costes común" in r["foot"] and "(3)" in r["foot"]
    assert "todas las que entran en el ranking son frágiles" in r["vacio"]
    md = r["md"]
    assert "1. Estrategia X" in md and "frágil" in md and "E2 · impulso (variante de 7 días)" in md and "descartada por ti" in md
    _sin_null(rows, r["foot"], r["vacio"], md)
    # sin datos del ranking (404) pero con «openings»: se nombra la que opera
    s = run(RK + """H.fetchReply = url => ({ok: false, status: 404});
      H.open(); reset(); H.recv({type: "openings", ts: now, params: {preset: "ruptura3", nombre: "Ruptura V3: rango de apertura"}});
      await loadApi(); S.tab = "ranking"; renderAll(true); const live = H.el("#rkLive").innerHTML;
      S.tab = "mando"; renderAll(true);
      return {live, md: H.el("#mdRk").innerHTML};""")
    assert "Ruptura V3: rango de apertura" in s["live"] and "sin datos del ranking" in s["live"] and "GET /api/ranking no existe" in s["live"]
    assert "Ruptura V3: rango de apertura · sin datos del ranking" in s["md"] and "Sin ranking" in s["md"]
    _sin_null(s["live"], s["md"])


# ---------------------------------------------------------------- Laboratorio vivo (docs/07, 13 y 14; docs/04 2.19)
# Con la forma de GET /api/lab_vivo (estado.json + mejores + diario + pausa_manual) y de sus subrutas.
LV = r"""
const H1 = "h-20261020-0900-ruptura_canal_48h";
const tarjeta = (id, o = {}) => ({id, hipotesis: id.split(":")[0], titulo: "Título " + id, familia: "ruptura_canal", regla: "Regla " + id, clase: "candidata",
  cumple_reglas: true, fuera_de_reglas: false, frecuencia: "frecuente", op_dia: 1.4, dentro: {n: 512, R: 0.08, ic95: [-0.05, 0.21]},
  fuera: {n: 330, R: 0.21, ic95: [0.04, 0.38], p: 0.003, q: 0.07, dsr: 0.97, mitades: [0.18, 0.24]},
  cuenta: {R_dia: 0.29, pct_dia: 0.145, pct_dia_compuesto: 0.144, caida_max_pct: 8.2},
  meta: {falta_R_dia: 1.71, falta_pct_dia: 0.855, texto: "Le faltan 1,71 R al día"}, motivos: [], puntuacion: 0.04, ...o});
const est = (o = {}) => ({formato: 1, ts: now - 5000, latido: now - 5000, estado: "pausa", motivo: "Pausa por la apertura de Europa hasta las 09:30",
  aviso: "Laboratorio de investigación: nada de esto opera.", proxima_pausa: {ventana: "europa", nombre: "Europa", desde: now - 600000, hasta: now + 1500000},
  recursos: {procesos: 0, procesos_max: 2, memoria_libre_mb: 2965, memoria_limite_mb: 2500, memoria_usada_mb: 0, cpu_pc_pct: 52.8, limitado_por: "pausa",
             prioridad: "por debajo de lo normal"},
  datos: {epoca: "2026-10", periodo: {desde: "2023-10-01", hasta: "2026-09-30", corte: "2025-08-16"}}, codigo: {canario: {estado: "ok"}},
  contadores: {hipotesis: {total: 41, hoy: 41, en_cola: 41, cupo_hoy: 41}, n: {total: 214, fuera: 61, efectivo: 38.4, previo: 3917, dsr: 2336},
               descartes_por_motivo: {R2: 9, K2: 3}},
  embudo: [{etapa: "recibidas", n: 41}, {etapa: "admitidas", n: 41}, {etapa: "pasan_rapida", n: 12}, {etapa: "pasan_completa", n: 3},
           {etapa: "candidatas", n: 1}, {etapa: "en_avance", n: 1}, {etapa: "listas", n: 0}],
  meta: {objetivo_pct_dia: 1, riesgo_pct: 0.5, objetivo_R_dia: 2, mejor: null, falta_R_dia: 2, falta_pct_dia: 1,
         texto: "Le faltan 1,71 R al día (0,86 % de la cuenta con 0,5 % de riesgo). El riesgo no se sube."},
  en_curso: [], cola: {pendientes: 42, por_tipo: {admision: 41, canario: 1}, siguientes: []},
  mejores: [tarjeta(H1 + ":V2"), tarjeta(H1 + ":V3", {clase: "descartada_robustez", no_ha_pasado: "no ha pasado: B1: p = 0,11",
            fuera: {n: 260, R: 0.09, ic95: [-0.06, 0.24]}, cuenta: {R_dia: 0.1, pct_dia: 0.05}})],
  en_avance: [tarjeta(H1 + ":V2", {clase: "en_avance", avance: {desde: now - 9 * 864e5, completas: 12, abiertas: 2, R_medio: 0.3, p_holm: null,
              caida_max_pct: 1.2, faltan: "28 operaciones y 19 días"}})],
  listas: [], consolidacion: {ts: now - 3600e3, umbral_dsr_z: 3.64, bh_m: 4131}, diario_hoy: {dia: madDay(now), lineas: ["13:55 Pausa por la apertura"]},
  errores: [], diario: {dia: madDay(now), hipotesis: {recibidas: 41, admitidas: 41, rechazadas: []}, variantes: {rapida: 48, completa: 6, robustez: 1},
  texto: ["Hoy: 41 hipótesis admitidas de 41 recibidas"], lecciones: [], generador: []}, pausa_manual: false, ...o});
const DET = {formato: 1, hipotesis: {id: H1}, estado: "terminada", variantes: [{id: H1 + ":V2",
  tarjeta: {...tarjeta(H1 + ":V2"), checks: {R1: true, K2: true, B1: false, X9: true, A1: "x"},
            avance: {desde: now - 9 * 864e5, completas: 12, abiertas: 2, R_medio: 0.3, curva: [[now - 5 * 864e5, 1.0], [now - 864e5, 3.6]]}},
  operaciones: {fuera: [{t: now - 300 * 864e5, R_f: 1.5}, {t: now - 200 * 864e5, R_f: -1}, {t: now - 100 * 864e5, R: 2.1}, {t: "x", R_f: 5}]}}]};
const AYER = madDay(now - 864e5);
const lvReply = (o = {}) => url => { const ok = d => ({ok: true, status: 200, json: async () => d});
  if (url === "/api/lab_vivo") return ok(o.estado || est());
  if (url === "/api/lab_vivo/cola") return ok({pendientes: 42, por_tipo: {admision: 41, canario: 1}, en_curso: [],
    fallidos_24h: [{tipo: "completa", objetivo: H1 + ":V4", intentos: 3, error: "excepción <x>"}],
    siguientes: [{tipo: "canario", objetivo: "canario", prioridad: 13}, {tipo: "admision", objetivo: "h-20261029-1130-h01-tsmom_diario", prioridad: 21}]});
  if (url === "/api/lab_vivo/generador?limite=10") return ok({notas: [{archivo: "2026-10-29-1215.json", resumen: "6 hipótesis nuevas"}], n: 1});
  if (url === `/api/lab_vivo/diario?dia=${AYER}`) return ok({dia: AYER, hipotesis: {recibidas: 24, admitidas: 21},
    texto: ["Hoy: 21 hipótesis admitidas de 24 recibidas"], lecciones: ["lección de ayer"]});
  if (url.startsWith("/api/lab_vivo/hipotesis?id=")) return ok(DET);
  return {ok: false, status: 404}; };
const lvHtml = () => ["#lvHead", "#lvCtl", "#lvMeta", "#lvFunnel", "#lvBest", "#lvDet", "#lvAvance", "#lvDiario", "#lvCola"].map(i => H.el(i).innerHTML).join("\n");
"""


def test_laboratorio_estado_pausa_meta_embudo_mejores_detalle_avance_diario_y_cola():
    """Al conectar se pide GET /api/lab_vivo; al abrir la pestaña, también la cola, las notas del generador, los diarios
    anteriores y el detalle de lo que está en avance. Se ve la pausa con su cuenta atrás, recursos, el botón Pausar y el
    aviso de que todo es virtual; la meta del 1 % (fuera de muestra y en avance) con la nota fija del riesgo; el embudo
    con el número de pruebas y el listón; las mejores con su IC y su motivo; y, al pulsar una, su curva y sus
    comprobaciones en verde o rojo (ninguna petición con un id fuera del patrón)."""
    r = run(LV + """H.fetchReply = lvReply(); H.open(); reset(); await loadApi(); const f0 = H.fetches.slice();
      H.fetches.length = 0; H.fire("click", {"data-tab": "lab"}); await loadLab(); renderAll(true);
      const f1 = H.fetches.slice(), a = Object.fromEntries(["#lvHead", "#lvCtl", "#lvMeta", "#lvFunnel", "#lvBest", "#lvAvance", "#lvDiario", "#lvCola"].map(i => [i, H.el(i).innerHTML]));
      const hora = madHM(now + 1500000);
      H.fire("click", {"data-lv-sel": H1 + ":V2"}); await lvDetFetch(H1); renderAll(true);
      const det = H.el("#lvDet").innerHTML, detH = H.el("#lvDet").hidden;
      H.fire("click", {"data-lv-sel": H1 + ":V2"}); renderAll(true);
      return {f0, f1, a, hora, det, detH, cerrado: H.el("#lvDet").hidden, sent: H.sent(), todo: lvHtml()};""")
    assert "/api/lab_vivo" in r["f0"] and not any(u.startswith("/api/lab_vivo/") for u in r["f0"])
    f1 = r["f1"]
    for u in ("/api/lab_vivo", "/api/lab_vivo/cola", "/api/lab_vivo/generador?limite=10", "/api/lab_vivo/hipotesis?id=h-20261020-0900-ruptura_canal_48h"):
        assert u in f1, u
    assert sum(u.startswith("/api/lab_vivo/diario?dia=") for u in f1) == 2
    a = r["a"]
    assert "En pausa" in a["#lvHead"] and f"Pausa: apertura de Europa, vuelve a las {r['hora']} (dentro de" in a["#lvHead"]
    assert "0 de 2 · por debajo de lo normal" in a["#lvHead"] and "Todo es virtual: nada opera." in a["#lvHead"] and "Canario: el libro reproduce" in a["#lvHead"]
    assert 'data-lv-acc="pausar"' in a["#lvCtl"] and ">Pausar</button>" in a["#lvCtl"]
    m = a["#lvMeta"]
    assert "Mejor fuera de muestra" in m and "+0,29 R/día" in m and "Mejor en avance" in m and "+0,40 R/día" in m and "2,00 R/día" in m
    assert "Le faltan 1,71 R al día (0,86 % de la cuenta con 0,5 % de riesgo)" in m and "Con 0,5 % de riesgo por operación (no se sube)" in m
    fu = a["#lvFunnel"]
    assert ">Recibidas</small><b>41</b>" in fu and "hoy 41" in fu and "hoy 48 medidas" in fu and ">Listas</small><b>0</b>" in fu
    assert "214 variantes medidas" in fu and "más 3917 pruebas previas" in fu and "hace falta un Sharpe diario ≈ 0,17 fuera de muestra" in fu
    b = a["#lvBest"]
    assert "Título h-20261020-0900-ruptura_canal_48h:V2" in b and "+0,21 R" in b and "+0,04 a +0,38" in b and "330 op." in b
    assert "no ha pasado: B1: p = 0,11" in b and "1,71 R/día" in b and "descartada en robustez" in b
    av = a["#lvAvance"]
    assert "28 operaciones y 19 días" in av and "<small>Días</small><b>9</b>" in av and "<small>Completas</small><b>12</b>" in av and "<svg" in av
    d = a["#lvDiario"]
    assert ">Hoy</time>" in d and ">Ayer</time>" in d and "<b>21 hipótesis admitidas de 24 recibidas</b>" in d and "lección de ayer" in d
    assert "13:55 Pausa por la apertura" in d and "29/10 12:15" in d and "6 hipótesis nuevas" in d
    c = a["#lvCola"]
    assert "42 trabajos: 41 de admisión, 1 de canario" in c and "prioridad 21" in c and "excepción &lt;x&gt;" in c
    assert "Qué falla más" in c and "R2 · pierde dentro" in c and "K2 · pierde fuera" in c
    det = r["det"]
    assert r["detH"] is False and "<svg" in det and "Fuera de muestra (backtest, 3 op.)" in det and "En avance (virtual, 2 puntos)" in det
    assert "✓ R1 · ≥ 120 op. dentro" in det and "✗ B1 · mejor que al azar" in det and "✓ K2 · gana fuera" in det and "X9" not in det
    assert "Regla h-20261020-0900-ruptura_canal_48h:V2" in det and "p 0,003 · q 0,07 · DSR 0,97" in det
    assert r["cerrado"] is True and r["sent"] == []
    _sin_null(r["todo"], det, *a.values())


def test_laboratorio_pausar_reanudar_orden_web_y_sin_conexion():
    """Pausar y reanudar van por el WebSocket ({type: "lab_vivo", accion}); sin conexión se avisa y no se pierde el clic.
    El resultado llega en «orden_web» (area lab_vivo), con su aviso; con la pausa manual puesta (pausa_manual del bot)
    el botón pasa a «Reanudar»; una acción desconocida no se envía y si el bot no contesta se dice."""
    r = run(LV + """H.fetchReply = lvReply(); reset();
      S.lv = est(); S.lvApi = "ok"; S.tab = "lab"; renderAll(true);
      H.fire("click", {"data-lv-acc": "pausar"}); const t0 = H.el("#toast").textContent, s0 = H.sent().length;
      H.open(); await loadApi(); S.tab = "lab";
      H.fire("click", {"data-lv-acc": "pausar"}); renderAll(true); const t1 = H.el("#toast").textContent, pend = H.el("#lvHead").innerHTML;
      H.recv({type: "orden_web", ts: Date.now(), area: "lab_vivo", accion: "pausar", id: null, ok: true, why: null}); const t2 = H.el("#toast").textContent;
      H.recv({type: "lab_vivo", ts: now + 1000, estado: "activo", motivo: "Pausa manual (archivo pausa_manual)", latido: now + 1000, pausa_manual: true,
              proxima_pausa: {}, contadores: {}, embudo: [], meta: {}, mejores: [], en_avance: [], listas: [], canario: "ok", recursos: {procesos: 0, memoria_libre_mb: 3000}});
      renderAll(true); const man = H.el("#lvCtl").innerHTML + H.el("#lvHead").innerHTML;
      H.fire("click", {"data-lv-acc": "reanudar"});
      H.recv({type: "orden_web", ts: Date.now(), area: "lab_vivo", accion: "reanudar", id: null, ok: false, why: "No estaba en pausa"}); const t3 = H.el("#toast").textContent;
      H.fire("click", {"data-lv-acc": "borrar"}); H.fire("click", {"data-lv-acc": "pausar"});
      H.timeouts.filter(x => x.ms === 10000).forEach(x => x.f());
      return {t0, s0, t1, pend, t2, man, t3, t4: H.el("#toast").textContent, sent: H.sent()};""")
    assert "Sin conexión con el bot" in r["t0"] and r["s0"] == 0
    assert "Pausa del laboratorio pedida al bot (no toca el paper ni tus reglas)" in r["t1"] and "pendiente de que el bot la confirme" in r["pend"]
    assert r["t2"] == "Hecho: laboratorio en pausa manual (no toca el paper)."
    assert 'data-lv-acc="reanudar"' in r["man"] and ">Reanudar</button>" in r["man"] and "Pausa manual: la pediste tú" in r["man"]
    assert r["t3"] == "El bot no lo ha hecho (laboratorio): No estaba en pausa."
    assert "El bot no ha confirmado la orden del laboratorio" in r["t4"]
    assert r["sent"] == [{"type": "lab_vivo", "accion": "pausar"}, {"type": "lab_vivo", "accion": "reanudar"}, {"type": "lab_vivo", "accion": "pausar"}]
    _sin_null(r["pend"], r["man"])


def test_laboratorio_sin_ruta_404_apagado_parado_y_campos_rotos():
    """Un bot sin GET /api/lab_vivo (404) no rompe nada, lo dice y no pide las subrutas; «apagado» enseña su nota y no
    ofrece mandos; «parado» (del bot, o porque el latido tiene más de 3 min) dice desde cuándo y lo último que hubo. Un
    resumen del bus con todo roto no rompe nada y un id de hipótesis con una ruta dentro nunca se pide."""
    r = run(LV + """H.fetchReply = url => ({ok: false, status: 404});
      H.open(); reset(); await loadApi(); S.tab = "lab"; await loadLab(); renderAll(true);
      const sinRuta = lvHtml(), api = S.lvApi, f404 = H.fetches.filter(u => u.startsWith("/api/lab_vivo/")).length;
      H.fetchReply = url => url === "/api/lab_vivo" ? {ok: true, json: async () => ({estado: "apagado", nota: "El laboratorio vivo no está en marcha (scripts/lab-vivo.ps1)", pausa_manual: false})} : {ok: false, status: 404};
      H.fetches.length = 0; await loadLab(); renderAll(true); const apagado = lvHtml(), fApag = H.fetches.slice();
      S.lv = null; H.fetchReply = lvReply({estado: est({estado: "parado", estado_corredor: "activo", latido: now - 10 * 60e3,
        nota: "El laboratorio vivo no da señales desde las 14:00 (más de 3 min)"})});
      await loadLab(); renderAll(true); const parado = lvHtml();
      S.lv = est({estado: "activo", latido: now - 10 * 60e3, ts: now - 10 * 60e3}); renderAll(true); const paradoWeb = H.el("#lvHead").innerHTML + H.el("#lvCtl").innerHTML;
      H.recv({type: "lab_vivo", ts: now + 50, estado: 5, motivo: {}, latido: "x", aviso: 7, proxima_pausa: "x", recursos: [],
        contadores: {n: "x", hipotesis: 3, descartes_por_motivo: {K2: "x", B1: -1}}, embudo: [null, 3, {etapa: 5, n: "x"}, {etapa: "recibidas", n: null}],
        meta: {mejor: 5, texto: 9, objetivo_R_dia: "x"}, canario: 5, pausa_manual: "si", nota: 4,
        mejores: [null, 7, {id: 5, fuera: "x", cuenta: {R_dia: "x"}, meta: 5, motivos: [null, 3]}, {id: H1 + ":V9", clase: 7, checks: 5}],
        en_avance: [{avance: {curva: [[1, "x"], null, "x"], desde: "x"}}, {id: "malo/../../x:V1", avance: {completas: "x"}}], listas: "x"});
      S.lvSel = H1 + ":V9"; renderAll(true); const rotas = lvHtml();
      H.fetches.length = 0; H.fire("click", {"data-lv-sel": H1 + ":V9"}); H.fire("click", {"data-lv-sel": "malo/../../x:V1"}); renderAll(true);
      const malo = H.el("#lvDet").innerHTML, fMalo = H.fetches.filter(u => u.includes("malo")).length;
      H.recv({type: "lab_vivo", estado: "apagado"}); renderAll(true);
      return {api, sinRuta, f404, apagado, fApag, parado, paradoWeb, rotas, malo, fMalo, fin: lvHtml()};""")
    assert r["api"] == "sin_ruta" and "aún no lee el laboratorio vivo (GET /api/lab_vivo no existe" in r["sinRuta"] and r["f404"] == 0
    assert ">Apagado</b>" in r["apagado"] and "no está en marcha (scripts/lab-vivo.ps1)" in r["apagado"] and "data-lv-acc" not in r["apagado"]
    assert r["fApag"] == ["/api/lab_vivo"]
    assert ">Parado</b>" in r["parado"] and "no da señales desde las 14:00" in r["parado"] and "Lo último que dijo: «Activo»" in r["parado"]
    assert "data-lv-acc" not in r["parado"]
    assert ">Parado</b>" in r["paradoWeb"] and "Sin latido desde hace más de 3 minutos" in r["paradoWeb"] and "data-lv-acc" not in r["paradoWeb"]
    assert "Sin curva: el id de la hipótesis no es válido." in r["malo"] and r["fMalo"] == 0
    _sin_null(r["sinRuta"], r["apagado"], r["parado"], r["paradoWeb"], r["rotas"], r["malo"], r["fin"])


def test_ranking_y_laboratorio_en_la_demostracion_son_ejemplos_marcados():
    """Sin bot, las dos pestañas enseñan datos de ejemplo incrustados (rankingData y labVivoData), rotulados como tales:
    el ranking en su orden, sin las que pierden, con la estrategia del motor simulado («6 diarias v2») aparte y fuera
    del ranking; el laboratorio con su cabecera, sus mejores, su detalle con curva y sus comprobaciones. Nada se envía."""
    page = HTML.read_text(encoding="utf-8")
    blob = lambda i: page.split(f'<script type="application/json" id="{i}">', 1)[1].split("</script>", 1)[0]
    rkd, lvd = json.loads(blob("rankingData")), json.loads(blob("labVivoData"))
    assert rkd["ejemplo"] is True and lvd["ejemplo"] is True and len(blob("rankingData")) + len(blob("labVivoData")) < 40_000
    assert all(re.fullmatch(r"h-\d{8}-\d{4}-[a-z0-9_-]{3,60}", h) for h in lvd["hipotesis"])
    assert "Ordenado por beneficio fuera de muestra con costes; lo de arriba con poca muestra o frágil suele ser suerte." in page
    # el texto tal cual de cada bloque, como lo daría el navegador (textContent): se lee por el camino de siempre
    bloques = json.dumps({i: blob(i) for i in ("rankingData", "labVivoData")}, ensure_ascii=False)
    r = run(f"const B = {bloques};\n" + """document.getElementById = id => id in B ? {textContent: B[id]} : null;
      demoRanking(); demoLab();
      H.fire("click", {"data-tab": "ranking"}); renderAll(true);
      const rows = H.el("#rkRows").innerHTML, ids = ["curso-ej", "lv-ruptura-canal-48h", "e2_impulso_7d", "ruptura3", "lab2000:ap_mom:15:0,2|rvol:1,5|todos|a_favor|todas|todas|todos|t:30|3|3|sig"].map(i => rows.indexOf(`data-rk-sel="${i}"`));
      const rk = {note: H.el("#rkDemoNote").innerHTML, noteH: H.el("#rkDemoNote").hidden, demo: H.el("#v-ranking").classList.contains("demo"), rows, ids,
                  live: H.el("#rkLive").innerHTML, badge: H.el("#rkBadge").hidden, foot: H.el("#rkFoot").innerHTML};
      H.fire("click", {"data-rk-sort": "op_dia"}); H.fire("click", {"data-rk-t": "fragiles"}); renderAll(true); rk.filtrada = H.el("#rkRows").innerHTML;
      S.tab = "mando"; renderAll(true); rk.md = H.el("#mdRk").innerHTML; rk.mdSub = H.el("#mdRkSub").textContent;
      H.fire("click", {"data-tab": "lab"}); renderAll(true);
      const lv = {note: H.el("#lvDemoNote").innerHTML, noteH: H.el("#lvDemoNote").hidden, demo: H.el("#v-lab").classList.contains("demo"), todo: lvHtml_()};
      H.fire("click", {"data-lv-acc": "pausar"}); lv.toast = H.el("#toast").textContent;
      H.fire("click", {"data-lv-sel": "h-20261020-0900-ruptura_canal_48h:V2"}); renderAll(true); lv.det = H.el("#lvDet").innerHTML;
      return {rk, lv, sockets: H.sockets.length, fetches: H.fetches.filter(u => u.includes("ranking") || u.includes("lab_vivo")).length};
      function lvHtml_(){ return ["#lvHead", "#lvCtl", "#lvMeta", "#lvFunnel", "#lvBest", "#lvAvance", "#lvDiario", "#lvCola"].map(i => H.el(i).innerHTML).join("\\n"); }""", live=False)
    k, v = r["rk"], r["lv"]
    assert k["noteH"] is False and "EJEMPLO" in k["note"] and k["demo"] is True and k["badge"] is True
    assert all(x >= 0 for x in k["ids"]) and k["ids"] == sorted(k["ids"])
    assert all(f'data-rk-sel="{i}"' not in k["rows"] for i in ("seis", "seis2", "fibo-ej"))   # las que pierden no entran
    assert "6 diarias v2" in k["live"] and "Fuera del ranking: pierde" in k["live"] and "Descartada por ti" in k["rows"]
    assert "frágil</span>" not in k["filtrada"] and 'data-rk-sel="lv-ruptura-canal-48h"' in k["filtrada"]
    assert "6 diarias v2 · fuera del ranking: pierde" in k["md"] and "(ejemplo)" in k["mdSub"]
    # las filas reales son las del bot: E2 con 7 días, frágil y sin corregir; Ruptura V3 con su Holm; cada una con sus costes
    assert ">sin corregir</span>" in k["rows"] and ">corregida Holm 1,00 ✗</span>" in k["rows"] and "costes del backtest" in k["rows"]
    assert "costes del laboratorio (sin funding)" in k["rows"] and "No hay un perfil de costes común" in k["foot"]
    assert "descartada por ti" in k["md"] and "frágil" in k["md"] and "cada una con los costes de su estudio" in k["note"]
    assert v["noteH"] is False and "DEMOSTRACIÓN" in v["note"] and v["demo"] is True
    assert "Activo" in v["todo"] and "Próxima pausa: apertura de Europa" in v["todo"] and "Todo es virtual: nada opera." in v["todo"]
    assert "Ruptura del canal de 48 h con la tendencia diaria a favor" in v["todo"] and "28 operaciones y 19 días" in v["todo"]
    assert v["toast"] == "Ejemplo: en la demostración no se envía nada al bot."
    assert "<svg" in v["det"] and "Fuera de muestra (backtest, 200 op.)" in v["det"] and "✓ B8 · Sharpe deflactado ≥ 0,95" in v["det"]
    assert r["sockets"] == 0 and r["fetches"] == 0   # nada que pedir sin bot
    _sin_null(*[x for x in (*k.values(), *v.values()) if isinstance(x, str)])


S4R = r"""
const S4R_P = {...SEIS_P, sl_pct: 2, target_r: 4, max_hold_h: 168, avg_hold_h: 35, tp1_r: null, tp1_frac: 0, lock_r: null, floor_r: null,
  trail_r: null, trail_atr: null, max_hold_active_h: null, preset: "seis4r", nombre: "6 diarias 4R: stop 2 %, objetivo 4R, hasta 7 días"};
"""
S4R_BT = {"generated": 1793300000000, "split": 1755295200000, "sl_pct": 2.0, "nota": "Réplica del motor con SEIS4R <b>",
          "nombre": "6 diarias 4R: stop 2 %, objetivo 4R, hasta 7 días",
          "params": {**SEIS_BT["params"], "sl_pct": 2.0, "target_r": 4.0, "max_hold_h": 168.0, "avg_hold_h": 35.0, "tp1_frac": 0.0,
                     "lock_r": None, "long_instrument": "spot", "short_instrument": "perp", "preset": "seis4r"},
          "fuera": {"n": 757, "mean_R": 0.1211, "win_rate": 0.2814, "max_R": 3.904, "paper_R_funding": 0.1228, "op_jornada": 2.474,
                    "salidas": {"stop": 540, "objetivo": 155, "tiempo": 62}, "horas_media": 44.0,
                    "largos": {"n": 391, "mean_R": -0.0613}, "cortos": {"n": 366, "mean_R": 0.3161},
                    "desde": 1755476100000, "hasta": 1792653300000, "curva_R": [[1755480600000, -1.1], [1792653300000, 91.69]],
                    "max_dd_R": 40.19, "total_R": 91.69, "ganadoras_n": 213, "ganadoras_bajo_2R": 42, "ganadoras_bajo_1R": 17,
                    "antes_de_costes": {"n": 757, "R_bruto": 0.2495, "deslizamiento_R": -0.0286, "comisiones_R": -0.0998,
                                        "funding_R": 0.0017, "R_neto": 0.1228, "costes_R": -0.1267, "gana_bruto_pct": 28.7,
                                        "op_jornada": 2.474, "cuenta_jornada_neto_pct": 0.152},
                    "perfiles": {"sin_costes": {"n": 757, "R": 0.2495}, "backtest": {"n": 757, "R": 0.1228},
                                 "okx_eu_con_xperps": {"n": 757, "R": 0.1495}, "okx_eu_sin_xperps": {"n": 736, "R": -0.4568}}},
          "dentro": {"n": 1562, "mean_R": -0.0478, "win_rate": 0.2273, "paper_R_funding": -0.0413, "desde": 1698624900000,
                     "hasta": 1755487200000, "curva_R": [[1698646140000, 1.0], [1755487200000, -74.62]], "max_dd_R": 172.13,
                     "largos": {"n": 815, "mean_R": 0.0202}, "cortos": {"n": 747, "mean_R": -0.122},
                     "antes_de_costes": {"n": 1562, "R_bruto": 0.0721, "deslizamiento_R": -0.0198, "comisiones_R": -0.1001,
                                         "funding_R": 0.0065, "R_neto": -0.0413, "costes_R": -0.1134, "op_jornada": 3.338,
                                         "cuenta_jornada_neto_pct": -0.0689},
                     "perfiles": {"okx_eu_con_xperps": {"n": 1562, "R": -0.015}, "okx_eu_sin_xperps": {"n": 1327, "R": -0.2286}}}}


def test_seis4r_nombre_descripcion_y_backtest():
    """Con el preset «seis4r» la web la nombra («6 diarias 4R»), explica qué hace y abre arriba su backtest tal cual: fuera
    de muestra gana (por los cortos), dentro pierde, y es la mejor de 378 salidas (frágil); la Ruptura V3 pasa a ser la
    anterior. Con otra en marcha, sale abajo como medida. El incrustado real (seis4rData) se pinta igual, sin huecos."""
    page = HTML.read_text(encoding="utf-8")
    r = run("const now = Date.now();" + FIAB + SEIS2 + RUP3 + S4R + f"""S.btImp = {{impulso: {json.dumps(IMP_BT)}, lab: null, seis: {json.dumps(SEIS_BT)}, seis2: {json.dumps(SEIS2_BT)}, ruptura3: {json.dumps(RUP3_BT)}, seis4r: {json.dumps(S4R_BT)}}};
      const out = {{}};
      for (const [k, m] of [["s4r", opn(S4R_P, {{params_source: "usuario"}})], ["rup3", opn(RUP3_P)]]){{
        S.openings = m; S.tab = "bt"; S.btSig = null; renderAll(true);
        const top = !H.el("#btS4rTopP").hidden;
        out[k] = {{top, alt: !H.el("#btS4rAltP").hidden, html: (top ? H.el("#btS4rTop") : H.el("#btS4rAlt")).innerHTML + H.el("#btS4rKpis").innerHTML,
          r3: !H.el("#btRup3P").hidden, r3title: H.el("#btRup3Title").textContent, r3html: H.el("#btRup3").innerHTML,
          e2: H.el("#btLiveTitle").textContent, e2sub: H.el("#btLiveSub").textContent, s2alt: !H.el("#btSeis2AltP").hidden}};
        S.tab = "market"; renderAll(true); out[k].sub = H.el("#openSub").textContent;
        S.tab = "trades"; renderAll(true); out[k].st = H.el("#tradesStrat").innerHTML; }}
      S.btImp = {{impulso: null, lab: null, seis: null, seis2: null, ruptura3: null, seis4r: null}}; S.openings = opn(S4R_P); S.tab = "bt"; S.btSig = null; renderAll(true);
      out.vacio = {{top: !H.el("#btS4rTopP").hidden, html: H.el("#btS4rTop").innerHTML}};
      return out;""", live=False)
    a, b = r["s4r"], r["rup3"]
    assert a["top"] and not a["alt"] and a["s2alt"] and a["r3"] and a["r3title"].startswith("Estrategia anterior: Ruptura V3")
    assert "Estuvo en tu cartera paper por tu decisión hasta que pasaste a «6 diarias 4R»" in a["r3html"]
    assert a["e2"].startswith("Estrategia anterior (E2)") and "«6 diarias 4R»" in a["e2sub"]
    for t in ("Fuera de muestra gana", "+0,12 R por operación en 757 operaciones", "(+0,12 R con funding)", "2,47 por jornada",
              "≈ +0,15 % de la cuenta por jornada", "caída de hasta −40,2 R (≈ −18,2 % de la cuenta, con el tamaño sobre el "
              "capital de cada momento, como el paper)", "Pero dentro de muestra pierde",
              "-0,05 R en 1562 operaciones", "llegó a caer 172 R (≈ 58 % de la cuenta, con el tamaño sobre el capital",
              "Frágil: es la mejor de 378 salidas",
              "la mejor fuera de muestra en % de la cuenta por jornada (el criterio del ranking; en R por operación, la 2.ª)",
              "elegida después de ver los datos", "No es una ventaja demostrada", "Fuera de muestra gana por los cortos",
              "cortos +0,32 R (366 op.) y largos -0,06 R (391 op.)", "largos +0,02 R y cortos -0,12 R",
              "Lo que se llevan los costes", "antes de costes +0,25 R por operación", "quedan +0,12 R",
              "con los costes de hoy del paper (OKX EU con X-Perps) da +0,15 R fuera",
              "sin cuenta X-Perps (contado 0,35 % por lado y sin cortos) pierde: -0,46 R fuera y -0,23 R dentro",
              "el objetivo de 4R (+8 %) se cobra en 20 % de las operaciones", "objetivo 4R 155", "7 días sin tocar ninguno 62",
              "la mejor, +3,90 R", "Tus reglas no se tocan", "o gana 4R o pierde 1R", "si a los 7 días no ha tocado ninguno",
              "42 de las 213 ganadoras", "Está en tu cartera paper por tu decisión", "Réplica del motor con SEIS4R &lt;b&gt;",
              "<small>Antes de costes</small>", "<small>Largos / cortos</small>", "stop 2,00 % y objetivo 4,0 R (+8,00 %)",
              "como mucho 7 días"):
        assert t in a["html"], t
    assert "sin tope de tiempo" not in a["html"] and "86 %" not in a["html"]   # la caída, con interés compuesto (58 %)
    assert a["sub"].startswith("Modo impulso «6 diarias 4R» (paper, por tu decisión)") and "como mucho 7 días" in a["sub"]
    assert "No llega a 6 al día: en el backtest hizo ≈ 2,5 operaciones por jornada fuera de muestra" in a["sub"]
    assert ("no llega a 6 al día: en el backtest hizo ≈ 2,5 operaciones por jornada fuera de muestra, cada posición ocupa "
            "su moneda hasta 7 días") in a["st"]
    assert "es la mejor de 378 salidas fuera de muestra (en % de la cuenta por jornada)" in a["st"]
    assert "6 diarias 4R: stop 2 %, objetivo 4R, hasta 7 días" in a["st"] and "preset 6 diarias 4R" in a["st"]
    assert "+0,12 R por operación fuera de muestra" in a["st"] and "Dentro de muestra pierde" in a["st"] and "frágil" in a["st"]
    assert "6 diarias 4R: 6 diarias 4R" not in a["st"]
    assert not b["top"] and b["alt"] and "Si la activas (scripts/activar_estrategia.py --preset seis4r)" in b["html"]
    assert b["r3title"].startswith("Estrategia en marcha: Ruptura V3") and "Ruptura V3: Ruptura V3" not in b["st"]
    assert r["vacio"]["top"] and "no trae el backtest de «6 diarias 4R»" in r["vacio"]["html"]
    _sin_null(a["html"], b["html"], a["sub"], a["st"], a["r3html"], r["vacio"]["html"])
    if 'id="seis4rData"' in page:   # el resumen real incrustado (scripts/incrustar_backtest.py)
        blob = page.split('<script type="application/json" id="seis4rData">', 1)[1].split("</script>", 1)[0]
        r2 = run("const now = Date.now();" + FIAB + S4R + f"""S.btImp = {{impulso: null, lab: null, seis: null, seis2: null, ruptura3: null, seis4r: {blob}}}; S.openings = opn(S4R_P); S.tab = "bt"; S.btSig = null; renderAll(true);
          return H.el("#btS4rTop").innerHTML + H.el("#btS4rKpis").innerHTML;""", live=False)
        for t in ("Fuera de muestra gana", "Pero dentro de muestra pierde", "Frágil: es la mejor de 378 salidas",
                  "Lo que se llevan los costes", "por tu decisión", "sin cuenta X-Perps", "Fuera de muestra gana por los cortos"):
            assert t in r2, t
        _sin_null(r2)


def test_seis4r_en_el_ranking_de_la_demostracion_marca_su_fila():
    """El mapa preset → fila de la web (RK_ID_PRESET) es el de bot/ranking.PRESET_FILA: con «6 diarias 4R» en marcha, la
    fila en marcha es la de su salida en salidas_motor.py (la n.º 1), no otra."""
    from bot import ranking
    r = run("return {map: RK_ID_PRESET};", live=False)
    assert r["map"] == ranking.PRESET_FILA


def test_tope_propio_dice_como_mucho_7_dias_y_sin_backtest_no_inventa_la_frecuencia():
    """Con tope_propio (los 7 días son el tope de la estrategia, como en «6 diarias 4R»: apertura.SEIS4R) la web dice
    «como mucho 7 días» aunque no sea el preset entero; sin él, los 7 días de E2 siguen siendo «de seguridad», como en el
    motor (apertura.hold_text). Sin el backtest de «6 diarias 4R», «Estrategia activa» no inventa cuántas hace al día."""
    r = run("const now = Date.now();" + FIAB + S4R + """
      const base = {...S4R_P, preset: null, nombre: null};
      const out = {propio: impParams({...base, tope_propio: true}), e2: impParams({...base, tope_propio: false})};
      S.btImp = {impulso: null, lab: null, seis: null, seis2: null, ruptura3: null, seis4r: null}; S.openings = opn(S4R_P);
      S.tab = "trades"; renderAll(true); out.st = H.el("#tradesStrat").innerHTML;
      return out;""", live=False)
    assert "como mucho 7 días" in r["propio"] and "seguridad" not in r["propio"]
    assert "sin tope de tiempo (7 días de seguridad)" in r["e2"] and "como mucho 7 días" not in r["e2"]
    assert "no llega a 6 al día: cada posición ocupa su moneda hasta 7 días;" in r["st"]
    _sin_null(r["propio"], r["e2"], r["st"])


def test_seis4r_solo_dice_lo_que_dicen_sus_datos():
    """Lo de «gana por los cortos», «al revés» y «la mayoría toca el stop» sale de las cifras del resumen: con otras cifras
    (los cortos también pierden, pocos stops) no se dice."""
    otro = {**S4R_BT, "fuera": {**S4R_BT["fuera"], "cortos": {"n": 366, "mean_R": -0.05},
                                "salidas": {"stop": 100, "objetivo": 600, "tiempo": 57}}}
    r = run("const now = Date.now();" + FIAB + S4R + f"""S.btImp = {{impulso: null, lab: null, seis: null, seis2: null, ruptura3: null, seis4r: {json.dumps(otro)}}};
      S.openings = opn(S4R_P); S.tab = "bt"; S.btSig = null; renderAll(true);
      return H.el("#btS4rTop").innerHTML;""", live=False)
    assert "Largos y cortos fuera de muestra" in r and "gana por los cortos" not in r and "al revés" not in r
    assert "La mayoría toca el stop" not in r and "<b>Salidas:</b>" in r
    _sin_null(r)
