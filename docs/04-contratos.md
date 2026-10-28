# Contratos entre módulos (fase 1b y fase 2)

Este documento es la referencia común. Cualquier módulo que publique o consuma datos debe respetar estos formatos al pie de la letra, porque la web (`dashboard/radar.html`) los lee tal cual.

## 0. Reglas comunes

- Python 3.11, asyncio y aiohttp. El entorno es `M:\BotFundamental-runtime\venv`.
  - No añadas dependencias, salvo `anthropic` en el módulo de la fase 2.
- **Un archivo por módulo** dentro de `bot/`, con sus pruebas en `tests/test_<modulo>.py`.
  - Las pruebas no usan la red: van con muestras reales recortadas como datos fijos.
  - Ejecuta las pruebas así: `M:\BotFundamental-runtime\venv\Scripts\python.exe -X utf8 -m pytest -q tests/test_<modulo>.py`.
- **No edites** `__main__.py`, `server.py`, `desk.py`, `filters.py`, `config.py`, `models.py`, `regions.py` ni `dashboard/radar.html`. La integración la hace el coordinador. Si necesitas algo de esos archivos, dilo en tu informe final.
- Tiempo:
  - `models.now_ms()` en milisegundos UTC.
  - `datetime` siempre con zona horaria; nunca `datetime.now()` sin `tz`.
- Datos:
  - Todo en `config.DATA_DIR`, que está en M: y fuera de OneDrive.
  - Las cachés de historia van en `config.HIST_DIR` y los resultados de backtest en `config.BACKTEST_DIR`.
  - Nunca escribas en C:.
- Publicar en la web: `bus.publish(dict)`.
  - Los mensajes con `"type": "event"` se guardan en SQLite y salen en el cable.
  - El resto de tipos se guardan como último estado y se reenvían a cada web que se conecta.
- Textos para el usuario siempre en español, con decimales con coma solo en los textos (los números del JSON van como números).
- Ninguna fuente de red puede tumbar el proceso:
  - Atrapa las excepciones por fuente.
  - Reintenta con espera creciente.
  - Registra el fallo con `logging`.
- Todo opera en **paper** (dinero ficticio con precios reales). Ningún texto puede dar a entender que hay dinero real.
- Las reglas del usuario (`config.USER_RULES`: stop ≥ 1,5 %, objetivo ≥ 2R y ≥ 3 % neto, sin break-even, stop dinámico ni parciales) no las cambia ningún módulo de los que miden y aprenden (aprendizaje, fuentes vivas, evolución): solo deciden qué noticias cuentan, cuánto pesan y qué estrategia (que las cumpla) opera el motor.

## 1. Interfaces que ya existen

```python
# bot/bus.py
class Bus:
    store: Store
    state: dict[str, dict]            # último mensaje por tipo: "market", "calendar", "sources", "agent:<id>", ...
    def publish(self, msg: dict) -> dict

# bot/store.py
class Store:
    db: sqlite3.Connection            # WAL; puedes crear tus propias tablas con CREATE TABLE IF NOT EXISTS
    def events_since(self, ts_ms: int, kinds: tuple[str, ...]) -> list[dict]
    def recent_events(self, n: int) -> list[dict]

# bot/models.py
@dataclass class Item: source, uid, title, url="", published=None (ms), coins_hint=[], symbols=[], tier=None, via=None, region=None, lang=None, outlet=None
# feeds añade a cada Item: estado ("activa|prueba|silenciada|retirada", el peor entre el feed y su medio), peso (1, 0,5, 0 o 0)
# y mejora (None o el estado peor con que otro feed entregó antes la misma entrada). Los cazadores no los ponen: activa, peso 1.
@dataclass class Source: id, name, tier, interval, fetch

# bot/desk.py
class NewsDesk:  def handle(self, item: Item, src: Source, warm: bool) -> bool   # filtra, agrupa repetidas y publica

# bot/hunters.py
def parse_rss(raw: bytes, source: str) -> list[Item]

# bot/regions.py
MARKETS: dict[str, Market]   # tokio, seul, shanghai, hongkong, dubai, frankfurt, londres, madrid, nuevayork, saopaulo
REGIONS: dict[str, (nombre, clave_bolsa | None)]   # jp, kr, cn, hk, me, de, uk, es, us, latam, global
def session(m, now) -> {"state": "abierta|pausa|preapertura|cerrada", "open_utc", "close_utc", "next_open_utc"}
def closes_before_open(a, b, day) -> bool;  def ordered_by_close(day) -> list[Market]

# bot/market.py  (ya publica "market": {"prices": {coin: float}, "flow": {coin: {ls, funding, funding_h, oi, taker, prem, pctl, label, extreme, ts}}, "spread"})
#   ts de cada fila del flujo: cuándo se leyó de Binance. market.fresh_flow(market, now) da solo las de ≤ 5 min (las demás
#   no se publican ni se usan: predicciones, motor, paper, entrada por noticia, estratega, /api/estado y briefing;
#   hallazgo n.º 44). Una fila sin ts (de antes del 28-10-2026) se acepta.
#   funding: última tasa de Binance USDT-M en % de SU periodo; funding_h: horas de ese periodo (GET /fapi/v1/fundingInfo cada
#   6 h; los símbolos que no lista liquidan cada 8 h). Sin respuesta de Binance no va funding_h y se supone 8 h
#   (market.funding_8h y paper.funding_8h escalan a % por 8 h). Es la tasa de Binance, no la de OKX X-Perps EU: aproximación.

# Módulos que miden y aprenden (todos solo paper; los arranca __main__ y un fallo en uno no para el resto: si uno no se
# puede construir, el bot sigue sin él (optional, también la vigilancia: sin ella, las velas salen solo de la caché), y
# cada bucle del bot corre supervisado: si se cae, se registra y se relanza con espera creciente, de 5 s a 5 min
# (__main__.supervise; hallazgos n.º 27 y 46). La conexión principal a bot.db espera hasta 30 s a un bloqueo)
# bot/vigilancia.py     Vigilancia(bus, store, session)                          → estado «vigilancia» (2.8)
# bot/aprendizaje.py    Aprendizaje(bus, store, local_ai, bars=CacheBars(live))  → estado «aprendizaje» (2.10)
#   source_weight(medio) -> float (1 = neutro, 0 a 1,5) y adjusted_credibility(cred, medio) -> int: síncronas y sin base
#   observe(evento): gancho síncrono del bus (__main__ envuelve bus.publish) que anota el medio de cada captada
# bot/fuentes_vivas.py  FuentesVivas(bus, store, session); attach(feeds) antes de feeds.run() → estado «fuentes_vivas» (2.12)
#   prioridad_triaje(prio, estado, story) -> float | None: prioridad de la IA local según el estado de la fuente (la usa la mesa)
# bot/evolucion.py      Evolucion(bus, store); conectar_fuentes(fv); conectar_aprendizaje(apr) → estado «evolucion» (2.11)
# bot/reaccion.py       Reaccion(bus, bars=CacheBars(live).get)                     → estado «cripto_aperturas» (2.14)
#   historia y encadenamiento en un hilo (al arrancar y una vez al día) → BACKTEST_DIR/reaccion_aperturas.json
# bot/reflejo.py        Reflejo(bus, bars=CacheBars(live).get); observe(evento): gancho síncrono del bus → evento «reflejo» (2.6)
#   aplica(titular, triaje, clase), cuenta(titular, triaje, reflejo, clase), macro_confirmada(…) -> bool y
#   clases(store o conexión, historias) -> {historia: clase de la captada}: las usan apertura, predict, server,
#   claude_agents y aprendizaje. clase = patch.clase de la captada; sin ella, solo el titular (nunca el triaje)
# bot/fiables.py        Fiables(bus, store)  (al arrancar y cada hora, en un hilo)    → estado «noticias_fiables» (2.15)
#   fiable_para(medio, feed, tipo, dir) -> dict del grupo FIABLE que pasa la puerta para operar (opera) o None;
#   monedas_de(titular, triaje, clase, reflejo) -> (monedas, motivo); hay_fiables() (¿alguno opera?), ultimo(),
#   usar(msg). Los usan apertura (veto de «6 diarias») y entrada_noticias
# bot/entrada_noticias.py  EntradaNoticias(bus, store, submit, get_portfolio, bars=CacheBars(live).get, get_motor)
#   get_motor() → parámetros del motor en marcha: los topes del día compartidos son los de su preset (E2: 4 y 2 %;
#   «6 diarias»: 6 y 3 %), nunca más de 6 entradas y 3 % de riesgo
#   observe(evento): gancho síncrono del bus → eventos «entrada_noticia» (2.16) y entradas en el paper («strategy» →
#   riesgo → aprobación → paper, como el motor). BOT_ENTRADA_NOTICIAS=0 la apaga
# bot/apertura.py       paper_day_entries(cartera, ahora) -> {story: riesgo %}: TODAS las entradas del paper del día de
#   Madrid; la usan el motor y la entrada por noticia (topes diarios compartidos). PRESETS {impulso, seis},
#   preset_of(params), preset_info(params, activa)
# bot/predict.py        Predictor(bus, store, get_trend, get_orderflow, get_market, get_backtest, get_reaccion=None)
#   get_reaccion() → último «cripto_aperturas» (tasa base de cada ventana y reacción de la apertura anterior)
#   registrar(store, tipo, sujeto, antes, despues, motivo, evidencia, origen) y leer(...): historial evolution_log (4)
#   apuntar_cambios_codigo(store): CAMBIOS_CODIGO (cambios del código que alteran el paper), una sola vez al arrancar
#   leer_estrategia_activa(), params_motor(activa), firma_motor(params): la estrategia que opera el motor (2.9)

# bot/local_ai.py (LocalAI: classify(title, src, tier, coins) -> dict; brief(window, when) -> str; puerto 8094, alias qwen-trading)
```

Monedas operables: `config.COINS = ["BTC", "ETH", "SOL", "XRP"]`, cotizadas en USDC (`config.QUOTE`) en OKX EU.

## 2. Mensajes nuevos (bus → web)

### 2.1 `sources_summary` (módulo feeds)
```json
{"type": "sources_summary", "ts": 0,
 "totals": {"feeds": 1830, "ok": 1712, "failing": 61, "waiting": 57, "items_last_hour": 2400, "relevant_last_hour": 310},
 "regions": [{"region": "jp", "name": "Japón", "market": "tokio", "feeds": 205, "ok": 198, "failing": 4, "waiting": 3,
              "items_last_hour": 260, "relevant_last_hour": 31, "median_lat_s": 140}]}
```
Se publica como mucho cada 10 s y solo si ha cambiado. El detalle por feed sale por `GET /api/fuentes?region=jp`, que devuelve una lista de `FeedState.view()`.
`totals.sin_red` (hallazgo n.º 44): `true` mientras el PC está sin red. Si en 60 s fallan por la red (sin conexión, tiempo
agotado, conexión cortada) al menos el 80 % de los hosts sondeados (y al menos 10), no se suman fallos ni pausas, se
prueba la conexión con un solo feed cada 15 s y, al volver, los fallos y pausas de red se ponen a cero y todos los feeds
se reprograman repartidos (antes tardaban hasta 30 min en volver).

### 2.2 `trend` (módulo tendencia: índices de las bolsas)
```json
{"type": "trend", "ts": 0, "markets": {
  "tokio": {"name": "Tokio", "index": "Nikkei 225", "symbol": "^N225", "state": "abierta|pausa|preapertura|cerrada",
            "price": 62693.16, "prev_close": 62907.84, "change_pct": -0.34, "day_high": 0, "day_low": 0,
            "mom_30m_pct": 0.12, "last_session": {"date": "2026-10-23", "change_pct": -0.34},
            "futures": {"symbol": "ES=F", "change_pct": 0.21} , "next_open_utc": 0, "close_utc": 0,
            "spark": [62700.1, 62710.3], "updated": 0, "error": null}}}
```
- `change_pct` se calcula sobre el cierre anterior.
- `last_session` es la última sesión completa, y sirve para replicar en las bolsas que abren después.
- `futures` vale null si la bolsa no tiene futuro.
- `spark` son los cierres de 5 minutos de la sesión, como mucho 120.
- Se consulta cada 60 s.

### 2.3 `orderflow` (módulo tendencia: libro de órdenes y operaciones de cripto)
```json
{"type": "orderflow", "ts": 0, "coins": {"BTC": {
  "imbalance": 0.12, "bid_vol": 3.1, "ask_vol": 2.4, "spread_bps": 0.4,
  "cvd_1m": 1.2, "cvd_5m": -3.4, "cvd_15m": 8.1, "buy_ratio_5m": 0.53,
  "vol_5m_usd": 4200000, "vol_ratio": 1.8, "ret_5m_pct": 0.05, "ret_15m_pct": -0.1, "ret_60m_pct": 0.4,
  "trend": "alcista|bajista|lateral", "venue": "OKX EU (libro) + Binance (operaciones)"}}}
```
- `imbalance` = (volumen comprador − volumen vendedor) / suma, en los 5 primeros niveles del libro de OKX EU. Va de −1 a 1.
- `cvd_*` es la diferencia entre compras y ventas agresivas, en unidades de la moneda. Sale de las operaciones al contado de Binance (`wss://data-stream.binance.vision/stream?streams=btcusdt@aggTrade/...`).
- `vol_ratio` es el volumen de los últimos 5 minutos frente a la media de 5 minutos de la última hora.
- Se publica cada 5 s.

### 2.4 `predictions` (módulo predicciones)
```json
{"type": "predictions", "ts": 0,
 "items": [{"id": "tokio-2026-10-26", "kind": "bolsa|cripto", "target": "tokio", "label": "Tokio (Nikkei 225)",
            "event": "apertura|sesion|ventana", "when_utc": 0, "horizon": "sesión|1h|4h",
            "prob_up": 0.62, "expected_pct": 0.4, "confidence": "baja|media|alta", "n_samples": 1234,
            "drivers": [{"factor": "Nueva York cerró +1,1 %", "effect_pp": 8.0, "source": "encadenamiento nuevayork→tokio"}],
            "note": "Texto corto en español"}],
 "accuracy": {"evaluated": 40, "hit_rate": 0.57, "by_kind": {"bolsa": {"evaluated": 30, "hit_rate": 0.6}, "cripto": {"evaluated": 10, "hit_rate": 0.5}}}}
```
- `effect_pp` son los puntos porcentuales que el factor suma o resta a la probabilidad de subida.
- `prob_up` parte de la tasa base y se ajusta con cada factor.
- Las predicciones no son órdenes.
- Se publican cada 300 s y se guardan en la tabla `predictions` para medir su acierto después.
- **Las de bolsa (`kind: "bolsa"`, la probabilidad de que el ÍNDICE cierre por encima de su apertura) están OBSOLETAS
  desde el 28-10-2026.** Siguen en el mensaje para no romper el contrato y para medir su acierto, pero ni la web ni la
  skill radar de OpenClaw las enseñan y el motor ya no las usa (`apertura._pred` solo lee la de la ventana cripto de la
  moneda; antes daba el 40 % del factor a las de los índices): al usuario le importa la cripto, no el índice. No se
  añaden factores nuevos.
- **Las de cripto ya no usan los cierres de los índices** (antes «Hong Kong cerró −0,28 % → −6,3 pp»). Sus factores:
  tasa base de la cripto a 4 h de la apertura (la de `cripto_aperturas`, 2.14, con velas de 1 min; si no hay, la del
  backtest), la reacción de la cripto en la apertura anterior del motor medida sin mirar el futuro (la última cuyas 4 h
  ya terminaron al empezar la ventana; solo pesa si mueve ≥ 2 puntos con p < 0,05; con las cifras de hoy no predice y sale
  en `note` con peso 0), su tendencia de 4 h, el flujo de órdenes, el minorista extremo y sus noticias.
- **Noticias:** media del tono (dirección/100 × magnitud/100) ponderada por la credibilidad, por la confianza
  Σcred / (Σcred + 1), menos el tono medio de las triadas de 24 h (si hay ≥ 20), con tope de ±10 puntos. Ya no es una
  suma: antes una sola noticia bajista llegaba al tope y todas las predicciones salían bajistas (hallazgo n.º 3 de
  `docs/auditoria-2026-10-28.md`). Las macro o geopolíticas solo cuentan si la cripto las reflejó (evento `reflejo`).
- `accuracy` (y cada bloque de `by_kind` y `by_horizon`) lleva `up_rate`: la proporción de veces que subió. «Siempre
  sube» acertaría `up_rate` y «siempre baja», 1 − `up_rate`: un `hit_rate` que no las supera no tiene mérito.

### 2.5 `portfolio` (módulo paper trading)
```json
{"type": "portfolio", "ts": 0, "equity": 10012.3, "start_equity": 10000, "day_pnl": 12.3, "day_pnl_pct": 0.12,
 "positions": [{"story": "n…", "coin": "SOL", "side": "long|short", "qty": 3.1, "entry": 104.9, "sl": 103.2,
                "trail_pct": 1.2, "high_water": 105.6, "opened": 0, "riskUsd": 50, "notional": 3250,
                "headline": "…", "strategy": "seguir-bolsa-anterior", "window": "Apertura Londres",
                "instrument": "spot|perp", "funding": 0.0, "tags": {}, "reconstruyendo": false, "huecos": null}],
 "pending": [{"story": "n…", "deadline": 0, "mode": "veto|manual", "coin": "SOL", "side": "long|short"}],
 "scheduled": [{"story": "n…", "at": 0, "expires": 0, "coin": "SOL", "side": "long|short"}],
 "closed": [ /* últimas 50 operaciones, formato de 3.2 */ ],
 "limits": {"risk_per_trade_pct": 0.5, "max_daily_loss_pct": 3, "max_positions": 4, "day_loss_pct": 0.4,
            "halted": false, "halt_until": null},
 "mode": "auto|veto|manual", "halted": false, "caps": ["halt", "cancel"], "cuenta": null}
```
- `mode`: modo de aprobación actual. Lo cambia la web con `{"type": "mode"}` (3.3).
- `halted`: parada manual del paper, puesta y quitada desde la web con `halt` y `resume` (3.3). No es lo mismo que `limits.halted`, que es la pausa automática por la pérdida máxima del día y dura hasta `limits.halt_until` (medianoche de Madrid).
- Quien envía tickets (fase 2 y motor de aperturas) mira antes `halted` y `limits.halted`: con cualquiera de los dos a `true`, `submit` da `riskko` seguro. Mientras dure, no llama al estratega de pago (la fase 2 usa sus reglas, con 0 tickets libres), no publica `strategy`, no envía nada y no lo cuenta como rechazo: solo espera y vuelve a mirar. La cartera sale al momento tras `halt` y `resume`.
- `caps`: mandos que este paper admite por el WebSocket además de `decision`, `close` y `mode`. La web solo ofrece detener y cancelar si aparecen aquí.
- Se publica cada 2 s si ha cambiado algo y, al momento, tras cada orden de la web (`decision`, `close`, `mode`, `halt`, `resume` y `cancel`).
- Tamaño por riesgo: el nominal se elige para que la pérdida al saltar el stop inicial no pase de `risk_per_trade_pct` del capital, contando la comisión de entrada, la de salida y el deslizamiento previsto al salir (medio diferencial, o 1 pb sin dato, más 2 pb). El tope es el 40 % del capital. Un hueco que salte más allá del stop no se puede prever.
- `riskUsd` sigue siendo el riesgo hasta el stop sin costes, que es la base de R (como en el backtest). Por eso una salida justo en el stop da algo peor que −1 R. El evento `filled` trae en `exec.stopLossUsd` la pérdida prevista en el stop con costes.
- `instrument`: `spot` (contado, por defecto) o `perp` (futuro perpetuo). En contado USDC no se puede ir corto: los cortos del motor de aperturas y de la fase 2 van como `perp`, y con las reglas del usuario un corto sin `instrument` también. Un perpetuo:
  - cobra o paga el **funding** en cada liquidación (00:00, 08:00 y 16:00 UTC), con la tasa de `market.flow[moneda].funding` escalada a 8 h con `funding_h` o, sin dato, la última conocida. Con tasa positiva los largos pagan y los cortos cobran;
  - `funding` (USDC, + cobrado, − pagado) es el acumulado de la posición; cada liquidación publica un `manage` con `exec.funding` y `exec.funding_n`;
  - cuenta su margen (la mitad del nominal) en la exposición, y el nominal total en perpetuos no pasa de 2 veces el capital (2:1);
  - la comisión es `config.FEE_PERP_PCT`: por defecto la misma que en contado (0,10 % por lado), la prudente; la real
    de X-Perps es 0,05 % (`BOT_FEE_PERP_PCT=0.05`). El contado usa `config.FEE_PCT`: 0,10 % con cuenta X-Perps (por
    defecto) o 0,35 % sin ella (`BOT_OKX_XPERPS=0`); docs/05, sección 21.
- `tags`: las etiquetas del ticket (2.6), tal cual.
- **Reglas del usuario en el propio paper** (con `config.USER_RULES`, para TODOS los orígenes: motor, entrada por noticia
  y fase 2): stop ≥ 1,5 %, objetivo ≥ 2R y objetivo ≥ 3 % neto de costes (`stopPct × tp2R − coste de ida y vuelta` ≥
  `config.RULE_MIN_NET_PCT`; con 0,10 % de comisión y 2 pb por lado, el coste es 0,24 %: 1,5 % × 2R deja 2,76 % y se
  rechaza; hallazgo n.º 23), y los topes del día de Madrid (`paper.day_caps`: los del preset del motor en marcha, E2 4
  entradas y 2 %, «6 diarias» 6 y 3 %, nunca más de 6 y 3 %), contando todas las entradas del paper del día. Antes la
  fase 2 podía saltárselos (revisión del 28-10-2026). Lo que no las cumple sale `riskko` con el motivo.
- `cuenta`: la cartera de prueba con nombre que deja `scripts/nueva_cartera_paper.py` (`{nombre, eur, eur_usdc, usdc,
  desde, ficticia}`; su `usdc` manda sobre `BOT_PAPER_EQUITY`) o `null`. Va también en `GET /api/resultados` (3.2).
- **Huecos** (hallazgo n.º 47): cada posición guarda la última vez que se gestionó con precio (`seen`, en la tabla
  `trades`; las guardadas antes, la hora del último evento de la base). Si pasa más de 2 min sin gestionarse (el bot
  parado o caído, o sin precios), no se gestiona (`reconstruyendo: true`) hasta reconstruir el hueco con las velas de
  1 min de Binance al contado (pasadas a la escala de OKX EU con el cociente de los dos precios al empezar el hueco):
  si tocó el stop, el objetivo o el tope de tiempo, se cierra a ese nivel y a esa hora (el stop con su deslizamiento y,
  si la vela abre más allá, a su apertura; si toca stop y objetivo en la misma vela, el stop), con el funding de las
  liquidaciones de por medio al precio de su hora. Si no, sigue en vivo y lo apunta en `huecos`
  `[{desde, hasta, velas, factor, completo, fuente}]` (y un `manage` «hueco revisado»). Sin velas completas tras 10
  intentos (uno por minuto), sigue en vivo con `completo: false`.

### 2.6 Eventos nuevos o ampliados (`"type": "event"`)

Todos llevan `kind`, `agent`, `story` (salvo `flow` y `brief`), `title`, `text`, `coins` y `patch`. El `patch` se fusiona en el expediente de la noticia, y la web lo pinta con estos campos exactos:

| kind | agent | patch |
|---|---|---|
| `captured`, `filtered` | `hunter` | `headline, src, srcId, via, outlet, tier, t0, lat, url, coins, pub, region, lang, fresh (bool), filter{ok, why}, topics[], srcState, srcWeight, clase, macro` |
| `corroborated` | `hunter` | `corrob[] {src, tier, lat, title, url, region, outlet, srcId, lang, pub, srcState, srcWeight}`; y `srcState, srcWeight` si la confirma una fuente con mejor estado que el de la historia |
| `srcstate` | `hunter` | `srcState, srcWeight`: la misma entrada llega por un feed con mejor estado (feeds la marca como `mejora`); la historia sube de estado sin duplicarse y vuelve a la cola de la IA local si su prioridad nueva es mayor |
| `triaged`, `lowprio` | `local` | `triage` (ver 2.7) |
| `verified`, `unverified` | `verifier` | `verify{score (0-1), checks[{ok, t}], why?, dt, llm}` |
| `impact`, `noimpact` | `impact` | `impact{dir (+1 o −1), mag (% esperado en main), conf (0-1), hz (texto), why, main (moneda), per{moneda: %}, markets{bolsa: %}, drop? (texto), llm}` |
| `strategy`, `nostrategy` | `strat` | `strat{coin, side, mode, risk (% del capital), stopPct, trailPct, timing ("now"/"window"), win (texto), winAt (ms o null), entryType, tp1R, tp2R, tp1Frac?, beR?, trailStartR?, maxAgeMs?, instrument? ("spot"/"perp"), tags?, winEnd?, holdLabel?, valid, inval, why, llm, none? (bool)}`; el motor de aperturas añade `tags` también en el patch |
| `riskok`, `riskko` | `risk` | `risk{checks[{ok, t}], ok, why?}` |
| `humanwait`, `humanok`, `humanko` | `human` | `human{mode, deadline?, by?, at?, no?}` |
| `scheduled`, `order`, `filled`, `manage`, `blocked` | `exec` | `exec{scheduled?, sent?, px, slip (pb), ms, notional, sl, tp1?, tp2?, trail?, stopLossUsd?, blocked?, why?, instrument, funding?, funding_n?}` (un `manage` con `funding` es una liquidación de funding de un perpetuo) |
| `closed` | `exec` | `result{coin, side, qty, entry, exit, why, pnl, pnl_pct, R, fees, opened, closedAt, riskUsd, notional, headline, strategy, window}`; el evento lleva `tone: "good"` o `"bad"` |
| `reflejo` | `flow` | `reflejo{estado ("pendiente"\|"confirmada"\|"contraria"\|"sin_reflejo"), mov_pct, min, ref ("BTC"\|"media"), umbral_pct, tarde?}` (ver abajo) |
| `entrada_noticia` | `strat` | `entrada_noticia{estado ("esperando"\|"confirmada"\|"caducada"\|"vetada"), motivo, moneda (o null: toda la noticia), dir (±1), lado, grupo{clave, tipo, nombre, sentido, n, n_historias, dias, acierto, acierto_bruto, ic90_bajo, opera}, ta, expira?, ts_triaje?, umbral_pct?, mov_pct?, min?, paper_story?, entrada?, stop?, objetivo?, instrumento?, riesgo_pct?}`; `story` = la de la noticia (ver 2.16) |

Prefiltro (`bot/filters.py`, 28-10-2026; `clase` en la captada y en la descartada relevante):
- `cripto`: nombra BTC, ETH, SOL o XRP (`coins` = esas y solo esas) o habla de cripto en general (`coins` = []).
- `macro`: tema macro o geopolítico fuerte: bancos centrales y tipos, inflación, aranceles, guerra, sanciones y, de
  empleo, macroeconomía y banca, solo los datos de las grandes economías y las crisis (`topics.FUERTE`: datos de empleo,
  PIB, recesión, deuda soberana, crisis financiera…), más la geopolítica y la energía de gran impacto en 14 idiomas
  (`geopolitica_fuerte`: estrechos y bloqueos, ensayos nucleares y misiles balísticos, ataques u ofensivas contra un
  país, golpes de Estado, ley marcial, pandemias; `energia_shock`: recortes de la OPEP y subidas o caídas del crudo de
  un 4 % o más). `coins` = [] y `macro: true`. Necesita reflejo para contar.
- `contexto`: el resto de temas de mercado (política, elecciones, empresas, tecnología, sucesos, energía, inmobiliario,
  economía local…). Se capta (cuenta para medir fuentes y medios) pero no va a la IA local, no lleva monedas y no se tría.
- Ya no se reparten las cuatro monedas a todo (hallazgo n.º 2). Lo demás (ruido, opinión, antiguo, fuera del universo)
  se descarta como siempre.
- Cripto en general incluye los exchanges (Binance, Coinbase, OKX, Bybit, Bitget, Upbit, Bithumb, Bitvavo…), y en chino
  `加密` con lo que lo hace cripto (加密市场, 加密行业, 加密營收…; suelto es «cifrado») y en coreano `코인` (salvo la
  lavandería, el karaoke y 코인마켓캡). «sol» suelto solo es SOL en mayúsculas, como `$SOL` o con contexto cripto.
- En la cola de la IA local, la cripto va un punto por delante de la macro del mismo nivel y frescura (`desk.priority`).

Reflejo en la cripto (`bot/reflejo.py`): solo para las historias que NO son de cripto y que el triaje dejó con dirección
(|dirección| > 15). Qué es de cripto lo decide la `clase` del prefiltro de la captada (`cripto`, o `macro` y `contexto`
que no lo son); sin clase (historias de antes del 28-10-2026), solo el titular (nombra una moneda o habla de cripto).
**Nunca el tipo, los temas ni el resumen del triaje** (revisión del 28-10-2026, hallazgo n.º 1: la IA local pone el tema
«cripto» o el tipo «seguridad» a noticias de la Fed o de guerra, y el 12 % de las macro con dirección se colaba sin
confirmar). Se mide desde la captura, con el cierre de cada vela de 1 min frente al de la última vela cerrada antes de la
captura, la media de las cuatro monedas (o BTC si falta alguna o si una se queda sin velas nuevas más de 2 min por
detrás de BTC); manda el primer toque. El umbral (`umbral_pct`) es el
mayor de 0,5 % y 2σ del movimiento de 60 min de esa misma referencia en las 24 h anteriores a la captura (sin 24 h de
velas, 0,5 %):
- `confirmada`: a favor de la dirección de la noticia, al menos el umbral, en los 60 min siguientes;
- `contraria`: al menos el umbral en contra;
- `sin_reflejo`: pasados 60 min con sus velas completas, ninguna de las dos; sin los 60 min completos (hueco de velas o
  reinicio) sigue `pendiente` hasta 3 h después de la captura y entonces se cierra con lo que haya (sin velas,
  `mov_pct: null`). Con `tarde: true`: se trió más de 60 min después de captarla y lo que hizo la cripto ya había pasado
  cuando el bot pudo usarla (revisión del 28-10-2026);
- `pendiente`: dentro de los 60 min (se publica una vez; luego el estado final, una vez). Puede llevar `ref` y
  `umbral_pct` nulos si aún no hay velas.
`mov_pct` es el movimiento (con signo, + = sube) en el momento del estado y `min` los minutos desde la captura. Solo las
`confirmada` cuentan en la señal del motor (`apertura._news` y `signal_getter`, que adjuntan el último `reflejo` y la
`clase` de cada triada; una macro confirmada de la clase `macro` sin monedas vale para las cuatro y para cualquier
ventana, como una global), en la balanza de la vista Mundo, en el contexto del estratega (`Phase2.recent_news`: no
entran; una sin confirmar tampoco despierta al estratega por impacto), en las predicciones y en la señal de las
operaciones que mide el aprendizaje (`aprendizaje.signal_news`, aportaciones). En `/api/noticias` salen con `reflejo` y,
si no están confirmadas, `credibilidad_ajustada: 0`. Las de cripto cuentan como siempre (sin reflejo).
- **Azar:** con el 0,5 % fijo, un momento cualquiera con una dirección al azar salía `confirmada` el 18,7 % de las veces
  (y `contraria` otro 18,6 %); con el umbral de 2σ, el 5,0 % (`reflejo.AZAR`; 6.000 casos con las velas de 1 min de
  junio a octubre de 2026). Con las macro reales de 5 días (1.892), el 2,9 % sale `confirmada` y el 5,1 % `contraria`
  (3,5 % en las mismas horas con una dirección al azar): la dirección del triaje no se cumple más que al azar. Una
  `confirmada` es un filtro, no una prueba de causa; la web lo dice en el expediente.
- Al arrancar se recuperan las triadas de las últimas 7 h (la señal y la balanza miran 6 h) sin estado final; las que ya
  no se pueden medir quedan `sin_reflejo`. La web trata como macro sin confirmar una historia con `clase: "macro"` (o
  `contexto`) y dirección aunque aún no le haya llegado el `reflejo` (no cuenta hasta `confirmada`), y en una macro usa la
  dirección del triaje (la que se confirmó), no la del impacto.

Estado de la fuente (`srcState`, `srcWeight`; lo decide `bot/fuentes_vivas.py` y lo marca feeds en cada entrada):
- `srcState`: `activa` (peso 1), `prueba` (0,5), `silenciada` (0) o `retirada` (0). El de una historia es el mejor de las fuentes que la traen: sube con `srcstate` o con una confirmación de una fuente mejor. Al subir, la mesa la vuelve a mandar al triaje con la prioridad de su estado nuevo si es mayor que la que tuvo (una retirada no se trió nunca); la IA local no repite una historia que ya espera en su cola (le sube la prioridad) ni una que ya trió. La fase 2 (`Phase2.recent_news`) lee también `corroborated` y `srcstate` para ver esa subida y pondera cada noticia por `srcWeight` × el peso aprendido de su medio.
- Las de fuentes silenciadas o retiradas se publican igual (así se siguen midiendo y pueden volver), pero no cuentan en la señal del motor (`apertura._news`), no gastan IA de pago en la fase 2 ni entran en su contexto, sus confirmaciones no son independientes (`/api/noticias`) y van al final de la cola de la IA local (`fuentes_vivas.prioridad_triaje`: 1 de cada 4 entra con prioridad de muestreo para poder medirlas; una retirada no se tría).
- `outlet`: el medio que firma la pieza (dominio, o el nombre si llega por Google News sin dominio conocido); es la clave de los pesos del aprendizaje.

Campos del ticket para el paper:
- `instrument`: `spot` o `perp` (2.5). La fase 2 lo pone siempre (`perp` en los cortos); el motor de aperturas, según `long_instrument` y `short_instrument`.
- `tags` (motor de aperturas): `lado`, `instrumento`, `noticia` (`a_favor|en_contra|sin_noticia`), `noticia_fuerza`, `noticia_fiable` (`a_favor|en_contra|ninguna`: noticias de un grupo fiable de las 6 h previas, 2.15) y `noticia_fiable_grupo` (nombre del grupo o null), `apertura_anterior`, `apertura_anterior_pct`, `btc`, `volatilidad_pct`, `impulso_pct`, `medida_min`, `rvol`, `estrategia_firma` (la versión de la estrategia que la abrió, `evolucion.firma_motor`), `origen: "motor_aperturas"` y `preset` (`impulso|seis|null`). El paper las copia en la posición y en la operación de 3.2.
- **Origen de las operaciones** (`origen` en el ticket y en sus `tags`, desde el 28-10-2026): `motor_aperturas` (motor de aperturas, story `a-<ventana>-<fecha>-<moneda>-<n>`, estrategia `impulso-…`) o `noticia_fiable` (entrada por noticia confirmada, 2.16: story `nf-<story de la noticia>-<moneda>`, estrategia `noticia-fiable-largo|noticia-fiable-corto`, ventana «Noticia fiable», `noticia` = la story de la noticia). Sin `origen`: la fase 2 o lo anterior al 28-10-2026.
- `tags` (entrada por noticia): `origen`, `story` (la de la noticia), `grupo` (clave), `grupo_nombre`, `grupo_tipo`, `acierto`, `ic90_bajo`, `umbral_pct`, `mov_pct`, `min_confirmacion`, `lado` e `instrumento`.

Salidas opcionales del ticket (`paper.exit_rules`). Un ticket sin estos campos se gestiona como antes: break-even a +1 R en la entrada, stop dinámico desde el principio y salida solo en `tp2R`.
- `beR`: R desde el que el stop pasa a break-even. `null` o ≤ 0 significa nunca. Si el campo está, el nivel cubre costes: entrada ± (coste de ida y vuelta + 2 pb), es decir, ±0,26 % con 0,10 % de comisión.
- `trailStartR`: R desde el que actúa el stop dinámico. `null` significa desde la entrada.
- `tp1R` + `tp1Frac`: salida parcial de esa fracción de la posición en tp1. 0 significa sin parcial.
- `tp2R`: objetivo final. `null` significa sin tope.
- `maxAgeMs` (solo `timing: "now"`): si la aprobación (veto o a mano) llega más tarde que esto desde el envío, el paper no la ejecuta (`blocked`). El motor de aperturas manda 60 000; sin el campo, como siempre.

Con parciales, el `closed` y la operación de 3.2 dan `qty` inicial, `exit` como precio medio de todas las salidas, y `pnl`, `fees` y `R` totales (R sobre el riesgo inicial). La operación de 3.2 añade `partials[]` y `exit_last`.

En `llm` se guarda el gasto de cada llamada: `{model, key, tin, cached, tout, cost}` (cost en $), con el mismo cálculo que la simulación de la web.

### 2.7 Puntuación de cada noticia (triaje v2, IA local)

El `patch.triage` sustituye al «bueno o malo» por varias notas:
```json
{"tipo": "macro|regulatorio|institucional|exchange|seguridad|on-chain|red|mercado|geopolítica|política|empresas|energía|catástrofe|otro",
 "temas": ["elecciones", "comercio"],
 "credibilidad": 72,
 "alcance": {"nivel": "local|nacional|regional|global", "puntuacion": 65},
 "temporalidad": {"horizonte": "minutos|horas|días|semanas|estructural", "urgencia": 80},
 "repercusion": {"direccion": -40, "magnitud": 55, "activos": ["BTC", "ETH"], "bolsas": ["tokio", "hongkong"]},
 "novedad": 70,
 "importancia": 7,
 "sentimiento": "alcista|bajista|neutral",
 "monedas": ["BTC", "ETH"],
 "resumen": "Titular traducido al español (máximo 200 caracteres)",
 "motivo": "Por qué esa importancia (máximo 160 caracteres)",
 "ms": 2100, "model": "Qwen3.5 9B (local)"}
```
- **Credibilidad (0-100):** probabilidad de que sea cierto y esté bien contado.
- **Alcance:** a cuántos mercados llega.
- **Temporalidad:** cuánto dura el efecto y qué urgencia tiene.
- **Repercusión:** cómo podría afectar. La dirección va de −100 (muy bajista) a +100 (muy alcista) y la magnitud de 0 a 100.
- **Novedad:** 0 si ya era conocido o estaba descontado.
- **Importancia (0-10):** nota global; por debajo de `config.TRIAGE_MIN` se archiva (`lowprio`).
- **Sentimiento:** se deriva de la dirección: > 15 alcista, < −15 bajista y neutral en medio.
- **Integración:** el coordinador ajusta la credibilidad con el nivel de la fuente y las confirmaciones, y el alcance con las regiones que lo publican.

### 2.8 `vigilancia` (prueba en avance de las estrategias del laboratorio, sin operar)

Estado (no evento) de `bot/vigilancia.py`. Vigila las robustas y prometedoras de la lista congelada (`BACKTEST_DIR/laboratorio-congelado-2026-10-26.json`, cambiable con `BOT_VIGILANCIA_CONGELADO`) y las de `laboratorio.json`, y tras cada una que no dure 7 días su **variante del usuario** (código `E2-7d`): la misma estrategia sin tope de tiempo, solo los 7 días de seguridad, que es la regla que opera el paper. La de `sig` sigue siendo la prueba de la hipótesis congelada. Todo con las MISMAS funciones del laboratorio y las velas de 1 min de Binance que van llegando. Son operaciones **virtuales**: no se envía nada al paper ni al exchange y no hay dinero, ni siquiera ficticio.
```json
{"type": "vigilancia", "ts": 0, "estado": "ok|arrancando|datos_atrasados|sin_datos|error|sin_estrategias|apagada", "nota": null,
 "aviso": "Operaciones VIRTUALES: …", "costes": "Costes del laboratorio: …", "evaluacion": "Cada apertura se evalúa unas 4 h después…",
 "desde": 0, "desde_dia": "2026-10-27", "congelada": "2026-10-26", "lista": "laboratorio-congelado-2026-10-26.json",
 "datos_hasta": 0, "ultimo_calculo": 0, "calculo_s": 0.4, "aperturas_evaluadas": 12,
 "aperturas": [{"key": "nuevayork-2026-10-27", "id": "nuevayork", "nombre": "Nueva York", "open_ms": 0, "monedas": ["BTC", "ETH", "SOL", "XRP"],
                "entradas": [{"estrategia": "ap_mom:15:0,2|…", "codigo": "E2", "moneda": "XRP", "lado": "largo|corto",
                              "motivo": "objetivo|stop|tiempo|abierta|sin_datos", "R": 2.92}]}],
 "proxima": {"key": "tokio-seul-2026-10-28", "nombre": "Tokio y Seúl", "open_ms": 0, "evalua_ms": 0},
 "estrategias": [{"id": "ap_mom:15:0,2|rvol:1,5|todos|a_favor|todas|todas|todos|t:30|3|3|sig", "codigo": "E2",
   "nombre": "Movimiento de los primeros minutos: …", "clase": "robusta|prometedora|variante",
   "origen": "congelada|laboratorio|variante", "variante_de": null, "en_paper": false, "desde": 0,
   "reglas": {"sl_pct": 3.0, "r": 3.0, "tp_pct": 9.0, "dur": "sig|24h|7d", "dur_texto": "hasta la misma apertura del día siguiente",
              "neto_pct": 8.76},
   "vivo": {"operaciones": 6, "abiertas": 1, "sin_datos": 0, "R_medio": 0.51, "R_total": 3.07, "ganadoras": 0.5, "pct_medio": 1.5,
            "R_medio_modelo": 0.48, "largos": 4, "cortos": 2, "por_motivo": {"objetivo": 3, "stop": 3, "tiempo": 0}, "ic95": [-1.1, 2.1]},
   "backtest": {"operaciones": 278, "R_medio": 0.1402, "ganadoras": 0.518, "pct_medio": 0.42, "largos": 0.44,
                "R_medio_cortos_perp": 0.24, "periodo": "2025-08-16 a 2026-10-23"},
   "abiertas": [{"moneda": "XRP", "lado": "largo", "apertura": "nuevayork-2026-10-27", "ventana": "Nueva York", "entrada_t": 0,
                 "entrada": 1.52, "stop": 1.47, "objetivo": 1.66, "R_latente": -0.12}],
   "ultimas": [{"moneda": "SOL", "lado": "corto", "apertura": "europa-2026-10-27", "ventana": "Europa", "entrada_t": 0, "salida_t": 0,
                "R": 2.95, "pct": 8.85, "motivo": "objetivo"}]}]}
```
- **Qué se vigila:** solo lo que cumple las reglas del usuario: stop ≥ 1,5 %, ≥ 2R, horquilla pura y objetivo de al menos un 3 % neto de comisión y deslizamiento de ida y vuelta en contado (`reglas.neto_pct`; 1,5 % × 2R da 2,76 % y no se vigila). La variante (`origen: variante`, `variante_de` = la estrategia de la que sale) cuenta desde el mismo día que ella; `en_paper` marca la que reproduce el preset IMPULSO del paper (`apertura.LAB_REF` con 7 días), solo mientras el paper opera ese preset (`vigilancia.paper_es_impulso`: con `estrategia_activa.json` de «6 diarias» o de un ajuste de la evolución, ninguna lleva `en_paper` y la web no pone «la regla que opera el paper»).
- **Cuándo cuenta:** cada estrategia desde la medianoche de Madrid siguiente a su congelación (`desde`), el único fuera de muestra virgen. Lo anterior se calcula como calentamiento (posiciones que ya estarían abiertas), se guarda con `cuerpo.previa = true` y no entra en ninguna cifra.
- **Cuándo se evalúa una apertura:** cuando hay 4 h de velas tras ella (las pide `laboratorio.coin_tables`). Cada operación lleva su hora real de entrada y de salida.
- **Resolución:** horquilla pura del laboratorio (stop u objetivo; si toca los dos en la misma vela, cuenta el stop; el objetivo exige superarse en 2 pb), o al acabar su duración (`tiempo`): la misma apertura del día siguiente, 24 h o los 7 días de seguridad. Una posición por moneda dentro de cada estrategia, también entre rondas. Una operación que cruza un hueco de datos de 15 min o más se anula, como en el laboratorio, pero una abierta solo se anula si su apertura y su moneda se han recalculado con velas que llegan hasta donde llegaba ella. Sin velas de las 4 monedas no se calcula nada (`estado: sin_datos`): un reinicio sin caché y con Binance caído no toca lo guardado. Tras un apagón de más de 120 días, una abierta que ya no se puede recalcular queda `sin_datos`: no cuenta (`vivo.sin_datos`) ni ocupa su moneda.
- **Costes:** largos en contado con los del laboratorio; cortos en perpetuo (comisión de perpetuo y el funding liquidado mientras están abiertos; el corto cobra el positivo). `R` = resultado neto / stop. `R_medio_modelo` es el mismo cálculo con los costes de contado del backtest: la comparación exacta con `backtest.R_medio`, que usa ese modelo también en los cortos (`backtest.R_medio_cortos_perp` es el R medio de los cortos del backtest como perpetuo). La web enseña las dos cifras en vivo.
- `vivo.ganadoras`: parte de las cerradas con resultado neto positivo. `ic95`: intervalo aproximado del R medio en vivo (normal, desde 5 operaciones); con pocas operaciones la comparación no dice nada.
- `backtest`: cifras fuera de muestra de `laboratorio.json` (`oos`). Una variante que no está ahí lleva la de una réplica aparte con el laboratorio (mismo periodo, corte y costes; `fuente` lo dice: no está congelada ni pasó sus filtros) o `null`.
- Se calcula en un hilo y solo cuando llegan velas nuevas o cambiadas (una vez por minuto); se publica cada minuto si ha cambiado algo. Un fallo de datos o de cálculo no para nada: se ve en `estado` y `nota`, se reintenta en la ronda siguiente y su traza sale en `bot.log` como mucho una vez cada 5 min. `arrancando` hasta la primera ronda; `apagada` con `BOT_VIGILANCIA=0`.
- Una vela de Binance cuenta como cerrada 5 s después de su cierre (el reloj del PC puede adelantar) y en cada ronda se vuelven a pedir los 2 últimos minutos (lo nuevo manda). Las velas se guardan en la caché `HIST_DIR/1m/<SIM>-1m-<AAAA-MM>-api.json` (formato de `backtest_aperturas`; un mes con zip oficial no se toca) solo cuando cerraron hace más de 2 min: nunca una a medias. El funding se refresca cada 4 h en `HIST_DIR/funding`, en una tarea aparte que no frena la ronda de velas.
- `BOT_VIGILANCIA=0` la apaga.

### 2.9 `openings` (motor de aperturas)

Estado (no evento) de `bot/apertura.py`, cada 3 s como mucho y solo si cambia. Dos modos (`strategy`): `impulso` (por defecto en vivo, la operativa del usuario) y `ruptura` (la v2, con los campos de siempre). En modo impulso:
```json
{"type": "openings", "ts": 0, "strategy": "impulso",
 "params": {"estrategia": "impulso", "measure_min": 15, "min_move_pct": 0.2, "entry": "t:30", "rvol_min": 1.5, "btc_filter": "a_favor",
            "news_mode": "no_contra", "windows": ["tokio-seul", "china", "europa", "saopaulo", "nuevayork"], "coins": ["BTC", "ETH", "SOL", "XRP"],
            "sl_pct": 3.0, "target_r": 3.0, "max_hold_h": 168, "min_net_pct": 3.0, "long_instrument": "spot", "short_instrument": "perp", "…": "…",
            "preset": "impulso|seis|null", "nombre": "E2 · impulso|6 diarias|<nombre de la evolución>|null", "descripcion": "…|null"},
 "params_source": "preset_impulso|preset_impulso+entorno|manual|evolucion|usuario", "notice": null, "paused": false,
 "estrategia_cargada": {"firma": "3f2a…", "ts": 0, "pausada": false, "fuente": "evolucion", "registro": "v…", "preset": "seis"},
 "states": ["esperando_apertura", "midiendo_impulso", "esperando_entrada", "en_posicion", "terminada"],
 "day": {"date": "2026-10-27", "entries": 1, "risk_pct": 0.5, "max_entries": 4, "max_risk_pct": 2.0},
 "windows": [{"id": "europa-2026-10-27", "name": "Europa", "markets": ["frankfurt", "londres", "madrid"], "open_utc": 0,
              "state": "esperando_entrada", "why": "Se sigue el impulso de los 15 primeros min…",
              "coins": {"BTC": {"state": "esperando_entrada", "step": "hora|retroceso|reanudacion|ruptura|null", "signal": 1,
                                "why": "…", "instrument": "spot|perp|null", "rvol": 3.0, "range": {"hi": 0, "lo": 0},
                                "news": {"dir": 0, "strength": 0.0, "why": "…", "label": "a_favor|en_contra|sin_noticia"},
                                "tags": {}, "levels": {"apertura": 0, "impulso_pct": 0.3, "maximo": 0, "minimo": 0, "extremo": 0,
                                                       "retroceso": null, "ruptura": null, "hora_entrada": 0, "rvol": 3.0,
                                                       "entrada": 0, "stop": 0, "objetivo": 0},
                                "context": {"atr_pct": 0, "atr_h_pct": 0, "btc_dir": 1, "prev_ms": 0, "prev_react": 0.1},
                                "plan": {"stopPct": 3.0, "tp2R": 3.0, "rr": 3.0, "ok": true, "why": "…", "instrument": "spot",
                                         "gross_pct": 9.0, "cost_pct": 0.24, "funding_pct": 0.0, "net_pct": 8.76,
                                         "entry": 0, "stop": 0, "target": 0},
                                "entries": 1, "results": [], "drivers": [], "strength": 0.0, "gate": null, "moving": null}}}]}
```
- **Reglas (modo impulso):** se mide el impulso de los primeros `measure_min` minutos de cada apertura; si pasa de ±`min_move_pct` %, se sigue con la entrada del laboratorio (`t:N`, `pb` o `brk`), con volumen relativo ≥ `rvol_min` y el filtro de BTC. Las noticias filtran (`news_mode`) y etiquetan. Horquilla pura de `sl_pct` % y `target_r` R sin tope de tiempo (`max_hold_h` = 7 días de seguridad), sin break-even, stop dinámico ni parciales, y solo si el objetivo deja ≥ `min_net_pct` % neto de comisiones, deslizamiento y funding esperado. Una posición por moneda; topes diarios de entradas y riesgo. Largos al contado, cortos en perpetuo.
- **Señal de noticias:** cada noticia triada pesa dirección × magnitud × credibilidad × decaimiento × `srcWeight`; la credibilidad sale multiplicada por el peso aprendido de su medio (`aprendizaje.source_weight`, 1 mientras no haya datos). Las de fuentes silenciadas o retiradas no cuentan (2.6).
- **Estrategia de la evolución:** en modo impulso, si existe `DATA_DIR/estrategia_activa.json` (lo escribe la evolución al aplicar o revertir) y cumple las reglas del usuario (`evolucion.leer_estrategia_activa`; si no, se ignora con un error en el registro), sus parámetros mandan sobre el preset y sobre `BOT_IMPULSO_PARAMS` (`params_source: evolucion`, o `usuario` si el archivo lo escribió `scripts/activar_estrategia.py`: `fuente: "usuario"`, como `estrategia_cargada.fuente`). Con `pausada`, el motor mide pero no abre entradas nuevas (`paused: true` y aviso en `notice`); las abiertas siguen en el paper. Se relee cuando cambia, cada minuto como mucho, y solo si ninguna apertura se está armando o está en su sesión de entrada: las aperturas aún no armadas se rehacen con los parámetros nuevos.
- `estrategia_cargada`: la versión que opera (`firma` = `evolucion.firma_motor` de sus parámetros, la misma que llevan los tickets en `tags.estrategia_firma`), desde cuándo (`ts`), si está pausada, de dónde salen los parámetros, el `registro` del historial que la aplicó y el `preset`. La evolución lo lee para saber si el motor ya opera la versión del archivo.
- **Presets (`apertura.PRESETS`, 28-10-2026):** `params.preset` dice a qué preset corresponden los parámetros en uso (`apertura.preset_of`: todos los de `PRESET_KEYS` iguales; con cualquier cambio de la evolución o de `BOT_IMPULSO_PARAMS`, `null`), y `params.nombre` / `params.descripcion` su nombre visible (sin preset, el `nombre` de `estrategia_activa.json`, o `null`).
  - `impulso` («E2 · impulso», el de siempre, por defecto): el de arriba.
  - `seis` («6 diarias»: «Impulso de los primeros 15 min sin filtros, stop 1,5 % y objetivo 3R, como mucho 24 h; veto de noticias fiables en contra»): `measure_min` 15, `min_move_pct` 0 (cualquier movimiento ≠ 0; un impulso de 0 exacto no se opera), `entry` `t:15` (al medir), `rvol_min` null, `vol_filter` `todos`, `btc_filter` `ninguno`, `news_mode` `fiables`, las 5 ventanas y las 4 monedas, `sl_pct` 1,5, `target_r` 3 (+4,5 %), `max_hold_h` 24, `min_net_pct` 3, `avg_hold_h` 13, largos al contado y cortos en perpetuo, `risk_pct` null (0,5 %), `max_day_entries` 6, `max_day_risk_pct` 3, `lab` null. Pierde en el backtest de 3 años (`seis_diarias_resumen.json`, 3 de la API): se mide en paper y solo se activa con `scripts/activar_estrategia.py --preset seis` (`--preset impulso` vuelve a E2), que escribe `estrategia_activa.json` y lo apunta en `evolution_log` (2.11).
- **`news_mode: "fiables"`:** veta la entrada si en las `news_hours` (6) horas previas hay una noticia de un grupo FIABLE (2.15, `fiables.fiable_para` con su medio, feed, tipo y sentido) que se aplica a esa moneda (`fiables.monedas_de`: la nombra o es una macro confirmada por la cripto) y va en contra del impulso. Solo cuentan los grupos que pasan la puerta para operar (2.15): sin ellos no veta nada. La señal (`signal_from`) lleva entonces `fiables: [{story, dir, ts, grupo, nombre, acierto, titular}]` (el campo no sale si no hay ninguna) y las etiquetas, `noticia_fiable`.
- **Topes diarios compartidos (28-10-2026):** `day.entries` y `day.risk_pct` cuentan TODAS las entradas del paper del día de Madrid (`apertura.paper_day_entries`: posiciones y cerradas abiertas hoy, pendientes y programadas; también las de la entrada por noticia y las de la fase 2) más las que el motor acaba de enviar. Antes solo contaban las del motor (`a-…`). La entrada por noticia (2.16) cuenta lo mismo.
- Con `max_hold_h` < 168 (24 h en «6 diarias»), el ticket lleva `valid` «como mucho 24 h (hasta el …)» y `holdLabel` «tope de 24 h»; con 168, como siempre.

### 2.10 `aprendizaje` (medida de noticias y medios, pesos y post-mortems)

Estado (no evento) de `bot/aprendizaje.py`, como mucho cada 3 min y solo si cambia. Mide cada noticia triada con las velas de 1 min (lo que hizo el precio después) y de ahí el acierto de cada medio, un **peso por medio** para la señal y una explicación de cada operación cerrada. No opera nada.
```json
{"type": "aprendizaje", "ts": 0, "estado": "ok|arrancando|sin_datos|datos_atrasados|error", "nota": null, "aviso": "…",
 "params": {"umbral_pct": 0.3, "min_casos": 20, "min_dias": 10, "peso_min": 0.0, "peso_max": 1.5, "…": "…"},
 "horizontes": [15, 60, 240, 1440],
 "totales": {"historias": 0, "filas": 0, "completas": 0, "pendientes": 0, "sin_datos": 0, "medios": 0, "medios_con_datos": 0,
             "postmortems": {"hechos": 0, "pendientes": 0, "error": 0}},
 "global": {"clave": "todas", "casos": 0, "independientes": 0, "acierto": 0.5, "ic90": [0.4, 0.6], "bruto": 0.5, "base": 0.5, "…": "…"},
 "medios_mejores": [{"medio": "reuters.com", "nombre": "Reuters", "nivel": "T1", "region": "us", "idioma": "en", "casos": 0,
                     "independientes": 0, "dias": 0, "acierto": 0.5, "ic90": [0.4, 0.6], "bruto": 0.5, "base": 0.5,
                     "ret_1h": 0.0, "ret_4h": 0.0, "primera": 0, "adelanto_min": 0, "ruido": 0.0, "fuera_de_tema": 0.0,
                     "piezas": 0, "ops": 0, "R": 0.0, "peso": 1.0, "peso_manual": null,
                     "veredicto": "acierta|falla|neutro|ruido|pocos_datos", "compatible": "si|no|sin_evidencia|pocos_datos",
                     "directo": true, "candidato": false, "nota": "…"}],
 "medios_peores": [], "medios_ruido": [], "medios_nuevos": [], "medios_candidatos": [],
 "por_nivel": [], "por_region": [], "por_idioma": [], "por_tipo": [], "por_ventana": [], "por_fase": [], "por_rol": [], "por_moneda": [],
 "datos_hasta": {"BTC": 0}, "ultimo_calculo": 0, "calculo_s": 1.3}
```
- **Honestidad estadística:** el peso sale del acierto **ajustado al mercado** (lo que habría acertado la tendencia del día no tiene mérito) y contraído hacia el 50 %; un caso independiente es un bloque de 4 h; hacen falta `min_casos` casos en `min_dias` días distintos, y el peso solo se aparta de 1 si el intervalo del 90 % deja fuera el 50 %. Mientras tanto todos los pesos valen 1 y la señal es la de siempre.
- **Peso de un medio:** de 0 a 1,5 (1 = neutro). Multiplica la credibilidad de sus noticias en la señal del motor y en `/api/noticias`. Cada cambio queda en `medios_peso_hist` (y la evolución lo copia a `evolution_log` como `peso_cambiado`). Se puede fijar a mano o deshacer desde la web (3.3).
- **Post-mortems:** explicación corta de cada operación cerrada (paper y vigilancia) con sus hechos; la redacta la IA local en baja prioridad (sin ella, `pendiente`). Es explicación, nunca decisión.
- `BOT_APRENDIZAJE=0` lo apaga.

### 2.11 `evolucion` (historial, campeón y aspirantes, propuestas e informe semanal)

Estado (no evento) de `bot/evolucion.py`, solo si cambia. Todo solo paper.
```json
{"type": "evolucion", "ts": 0, "estado": "ok|arrancando|sin_datos|error", "nota": null, "modo": "proponer|automatico", "aviso": "Solo paper: …",
 "reglas": {"stop_min_pct": 1.5, "rr_min": 2.0, "objetivo_neto_min_pct": 3.0, "texto": "…"}, "criterios": {"texto": "…", "…": "…"},
 "campeon": {"nombre": "…", "codigo": null, "lab_id": "…", "fuente": "preset|evolucion", "pausada": false, "desde": 0, "params": {},
             "efectivos": {"sl_pct": 3.0, "target_r": 3.0, "windows": [], "coins": [], "news_mode": "no_contra"},
             "firma": "…", "confirmado": true, "pendiente_reinicio": false,
             "motor": {"confirmado": true, "texto": "…", "firma_motor": "…", "pausa_confirmada": true}, "reglas": {"ok": true, "fallos": []},
             "vivo": {"operaciones": 0, "R_medio": null, "R_contraido": 0.0, "ganadoras": null, "ic95": null, "muestra": "…"},
             "en_curso": 0, "sin_resolver": 0, "curva": [{"semana": "2026-W44", "lunes": 0, "operaciones": 0, "R": 0, "R_acum": 0}],
             "gemelo": {"id": "ap_mom:…|7d", "codigo": "E2-7d", "filtro": null, "texto": null}, "virtual": {}, "curva_virtual": [],
             "brecha_paper_virtual": {"texto": "…"},
             "degradacion": {"evaluada": false, "R_contraido": null, "ic95": null, "p_menor": null, "evidencia_clara": false}, "nota": null},
 "aspirantes": [{"id": "ap_mom:…|7d", "codigo": "E1-7d", "nombre": "…", "clase": "prometedora|variante", "compatible": true, "por_que_no": null,
                 "vivo": {}, "en_curso": 0, "curva": [],
                 "frente": {"referencia": "virtual|paper", "operaciones": 0, "R_medio": null, "ref_R_medio": null, "dif_R": null, "p": null,
                            "p_max_corregido": null, "pruebas": 0, "paper_R_medio": null, "cumple": false, "faltan": ["…"],
                            "confirmacion": {"estado": "en_observacion|esperando|confirmada|no_confirmada", "texto": "…"}}}],
 "propuestas": [{"id": "p…", "ts": 0, "tipo": "estrategia_promovida|estrategia_degradada|parametro_cambiado", "sujeto": "…", "titulo": "…",
                 "texto": "…", "cambio": {}, "evidencia": {}, "actualizada": 0, "bloqueada": null, "automatica": false}],
 "recientes": [{"id": "p…", "ts": 0, "tipo": "…", "titulo": "…", "estado": "aplicada|descartada|caducada|revertida", "resuelta_ts": 0, "por": "…", "nota": "…"}],
 "bloqueos": [{"clave": "…", "ts": 0, "motivo": "descartada|revertida", "por": "…", "propuesta": "p…"}],
 "cambios": [/* últimas 30 entradas de evolution_log, formato de GET /api/evolucion/historial (con reversible) */],
 "informe": {"id": "v…", "ts": 0, "semana": "2026-W44", "titulo": "…", "resumen": "…"},
 "ultima_evaluacion": 0, "proxima_evaluacion": 0, "ultimo_cambio": 0, "automatico_desde": null,
 "ultima_orden": {"accion": "aplicar", "id": "p…", "ok": true, "why": null, "ts": 0}}
```
- **Historial (`evolution_log`):** todo lo que cambia el sistema, con fecha, tipo, sujeto, antes, después, motivo en español, evidencia (cifras y tamaño de muestra), origen y, si deshace otro, `revierte`. Solo se añade: SQLite impide borrar o modificar filas. Tipos: `fuente_añadida`, `fuente_silenciada`, `fuente_retirada`, `fuente_restaurada`, `peso_cambiado`, `estrategia_promovida`, `estrategia_degradada`, `parametro_cambiado` (también el cambio de modo), `informe_semanal`, `propuesta_descartada` (una propuesta que descartaste desde la web; lo descartado queda bloqueado), `veredicto_medio` (el aprendizaje confirma otro veredicto de un medio sin cambiar su peso: filas `tipo='veredicto'` de `medios_peso_hist`), `estrategia_cambiada` (origen `usuario`: la estrategia que cambia el usuario con `scripts/activar_estrategia.py`, p. ej. «Estrategia cambiada por decisión del usuario el 28-10-2026: 6 diarias (…)», con la estrategia entera antes y después; se revierte como las de la evolución, y su reversión es otra `estrategia_cambiada` con `revierte`) y `filtro_cambiado` (origen `codigo`: un cambio del código que altera lo que filtra la estrategia en paper; `evolucion.CAMBIOS_CODIGO`, apuntado una sola vez al arrancar con su id fijo; el del 28-10-2026 es `codigo-noticias-2026-10-28`, el filtro de noticias y las predicciones de los índices, y `codigo-entrada-noticias-2026-10-28`, la entrada por noticia fiable y los topes del día compartidos; no se revierten desde la web). Las operaciones con `tags.origen` `noticia_fiable` nunca cuentan como del campeón (`evolucion.ORIGENES_AJENOS`), aunque no lleven firma y el campeón sea el preset. Cada entrada leída lleva `etiqueta` y `revertida_por`; las del mensaje y de `/api/evolucion/historial` llevan además `reversible` (la web solo ofrece «Revertir» entonces: estrategia y modo, y lo de fuentes_vivas y aprendizaje; nunca el informe, un veredicto ni una propuesta descartada). Una reversión de peso desde la web (`revertir_peso`) lleva «n.º N» en el motivo y queda enlazada (`revierte`).
- **Campeón:** la estrategia que opera el motor (`impulso-…` en el paper). Su referencia es su gemelo virtual de la vigilancia (misma salida, reglas y costes que los aspirantes); el paper es una segunda comprobación. Hasta que el motor confirma la versión del archivo (`openings.estrategia_cargada`, 2.9) sale `pendiente_reinicio` (la web dice «Pendiente de carga»: el motor la carga solo en ≤ 1 min, sin ninguna apertura en curso; no hace falta reiniciar) y no se evalúan su degradación ni sus ajustes. `desde` es cuando se aplicó esa versión (`estrategia_activa.json`) o, con el preset, su primera operación; la hora de carga del motor solo confirma la versión.
- **Aspirantes:** las estrategias de la vigilancia que el motor sabe operar con su salida. Para sustituir al campeón: ≥ 40 operaciones nuevas y completas, ventaja con p < 0,05 corregida por Holm, gemelo con ≥ 20 operaciones, reglas del usuario y confirmación con datos posteriores. Pausa propuesta: R contraído < −0,3 con ≥ 30 operaciones y evidencia clara.
- **Modo `proponer`** (por defecto): nada se aplica solo. **Modo `automatico`** (`BOT_EVOLUCION_MODO` o desde la web): promociones (una cada 14 días como mucho) y pausas se aplican solas; los ajustes de parámetros son siempre propuestas. Lo aplicado se escribe en `DATA_DIR/estrategia_activa.json`, que el motor carga entre aperturas (2.9).
- **Informe semanal:** el lunes a las 7:00 de Madrid, con 7 secciones en español (incluidos los medios que suben y bajan). Tras arrancar, la evaluación y el informe esperan (como mucho 15 min) a que la vigilancia publique sus estrategias, para no guardar uno sin aspirantes ni gemelo; con la vigilancia apagada o sin lista, no se espera. `BOT_EVOLUCION=0` la apaga.

### 2.12 `fuentes_vivas` (estado de cada fuente y descubrimiento de medios)

Estado (no evento) de `bot/fuentes_vivas.py`: tras cada revisión (cada hora) y al momento tras cada acción, solo si cambia.
```json
{"type": "fuentes_vivas", "ts": 0, "auto": true, "descubrir": true,
 "feeds": {"activa": 5600, "prueba": 3, "silenciada": 100, "retirada": 11}, "medios": {"activa": 0, "prueba": 0, "silenciada": 2, "retirada": 0},
 "candidatos": {"pendiente": 0, "verificado": 0, "anadido": 0, "sin_rss": 0, "bloqueado": 0, "en_registro": 0, "error": 0},
 "ruido_medio": 0.6, "sugerencias": 0, "revision": {"ultima": 0, "s": 2.1, "medidas": 0, "cambios": 0, "anadidas": 0, "nota": null},
 "cambios": [{"ts": 0, "id": "jp-nhk-…|medio:dominio", "nombre": "…", "accion": "anadir|silenciar|retirar|restaurar|activar|a_prueba",
              "texto": "…", "antes": "activa", "despues": "silenciada", "tipo": "…", "motivo": "…", "cifras": {}, "auto": true, "por": "…",
              "deshacer": {"accion": "restaurar", "id": "…"}}],
 "aviso": "…"}
```
- **Ciclo de vida:** `prueba` → `activa` → `silenciada` → `retirada` (y vuelta con `restaurar`). Una retirada no se sondea; una silenciada se sondea menos y sus noticias llegan con peso 0; una en prueba, con peso 0,5. Un **medio** (`medio:<dominio>`) vale para todos sus canales (feed propio, Google News y relays). El acierto se juzga por medio; un feed solo por la técnica y el ruido.
- **Reglas** (cada hora, con casos independientes = horas distintas en ≥ 7 días, acierto ajustado al mercado y contraído): silenciar con ≥ 30 casos y acierto claramente bajo lo neutro, o ruido ≥ 90 %, o caída técnica de ≥ 72 h; restaurar con ≥ 20 casos sin malos resultados; retirar tras ≥ 30 días silenciada solo si se confirma otra vez. Como mucho 3 bajadas por revisión y 10 en 24 h; las oficiales (T0) solo por caída técnica o a mano; lo que decides a mano se respeta 30 días. `BOT_FUENTES_AUTO=0`: las reglas solo sugieren.
- **Descubrimiento:** medios que llegan por agregadores con ≥ 10 casos medidos que dan las noticias antes que nadie o aciertan; se busca y verifica su RSS (robots.txt, como mucho 12 peticiones por hora) y se añade en `prueba`. `BOT_FUENTES_DESCUBRIR=0`: sin peticiones.
- Cada cambio queda en `source_status_log` y en `evolution_log` con su motivo y sus cifras, y se deshace volviendo exactamente al estado de antes. No toca las reglas de operación: solo decide qué noticias cuentan.
- Una orden a mano solo vale para una fuente conocida: un feed del registro, una fila guardada o un `medio:<dominio>` con dominio válido y ya visto (con cifras, con feed propio, con estado en los feeds, en `fv_noticias`, en `medios_info` o candidato). Si no, `orden_web` con `ok: false` y «No conozco esa fuente».

### 2.13 `orden_web` (resultado de una orden de la web sobre fuentes o pesos)

```json
{"type": "orden_web", "ts": 0, "area": "fuentes|aprendizaje", "accion": "silenciar|…|peso|revertir", "id": "medio:x.com", "ok": true, "why": null}
```
Estado (no evento) con el resultado de la última orden de 3.3 sobre fuentes o pesos de medios: la web lo enseña como aviso (solo el de la orden que tiene pendiente: «Hecho» o el `why` del rechazo). Las órdenes de la evolución dejan su resultado en `evolucion.ultima_orden`.

### 2.14 `cripto_aperturas` (la cripto en cada apertura de bolsa)

Estado (no evento) de `bot/reaccion.py`: cada minuto, en el estado inicial del WebSocket y en `GET /api/cripto_aperturas`.
Lo que importa no es el índice, sino cómo se mueve la cripto cuando abre cada bolsa.
```json
{"type": "cripto_aperturas", "ts": 0, "coins": ["BTC", "ETH", "SOL", "XRP"],
 "markets": {"tokio": {"name": "Tokio", "window": "tokio-seul", "state": "por_abrir|en_curso|terminada",
   "open_ms": 0, "next_open_ms": 0, "minutes_since_open": 90,
   "coins": {"BTC": {"ret_pct": 0.29, "range_pct": 0.75, "high_pct": 0.5, "low_pct": -0.25, "spark": [[0, 0.0], [0, 0.12]]}},
   "last": {"open_ms": 0, "coins": {"BTC": {"ret_4h_pct": -0.41, "range_4h_pct": 0.9}}},
   "hist": {"n": 729, "desde": "2023-10-30",
            "coins": {"BTC": {"up_rate": 0.48, "mean_ret_pct": -0.02, "mean_range_pct": 1.54, "range_ge2_rate": 0.2,
                              "range_ge2_8h_rate": 0.36, "n": 729}},
            "any_range_ge2_rate": 0.71, "any_range_ge2_8h_rate": 0.89}}},
 "chain": {"after_up_rate": 0.49, "after_down_rate": 0.512, "base_rate": 0.501, "n": 15156, "n_up": 7501, "n_down": 7655,
           "nota": "… no predice …", "parcial": {"after_up_rate": 0.485, "…": 0}, "tal_cual": {"after_up_rate": 0.597, "…": 0}},
 "total": {"n": 3791, "desde": "2023-10-30", "coins": {"…": {}}, "any_range_ge2_rate": 0.76, "any_range_ge2_8h_rate": 0.93},
 "hist_generated": 0}
```
- Claves de `markets`: las de `regions.MARKETS` (las mismas que la web usa en `MK` y `S.trend`). `window`: la ventana del
  motor (`apertura.openings_on`); `null` en Dubái, que no es una ventana.
- `state`: `en_curso` de 0 a 4 h desde la apertura (la ventana de reacción de la cripto); `terminada` pasadas 4 h y hasta
  la siguiente apertura; `por_abrir` solo si no abrió en las últimas 24 h o si hoy (fecha local) no tiene sesión.
  Horarios con cambio de hora y festivos (`regions`, `apertura.market_open_on`); São Paulo el miércoles de ceniza, a las 13:00.
- `open_ms`: la apertura de la sesión en curso o de la última; `minutes_since_open`: `null` en `por_abrir`.
- `coins` (`null` en `por_abrir` o sin velas): `ret_pct` desde la apertura (en curso) o a las 4 h (terminada); `range_pct`
  = (máximo − mínimo) / apertura en %; `high_pct` y `low_pct` sobre la apertura; `spark` ≤ 60 puntos [ms del cierre de la
  vela, % desde la apertura], empezando en [open_ms, 0]. La apertura es la de la primera vela de 1 min (como mucho 5 min
  tarde). Velas de Binance al contado: las de la vigilancia en memoria y la caché de `HIST_DIR/1m`.
- `last`: la última apertura completa distinta de la que va en `coins` (en `por_abrir`, la última), con `ret_4h_pct` y
  `range_4h_pct`; su `coins` es `null` sin velas.
- Un resultado «a las 4 h» (`coins` de `terminada` y `last`) solo lleva una moneda si sus velas llegan hasta 5 min antes
  de las 4 h, como la historia; con un hueco (un reinicio, o la caché por detrás) esa moneda no sale (nunca un valor a
  medias) y la bolsa lleva `aviso` (texto en español). En `en_curso` va lo que haya.
- `hist` (`null` hasta que se calcula): las aperturas de esa bolsa con su propia hora en los últimos 3 años como mucho
  (hasta ayer), sin sesiones especiales y con los festivos de los índices diarios. `up_rate`: proporción de aperturas en
  que la moneda sube a las 4 h; `mean_ret_pct` y `mean_range_pct` a 4 h; `range_ge2_rate` y `range_ge2_8h_rate`: rango
  ≥ 2 % a las 4 y a las 8 h (con ≥ 50 velas por hora). Extras: `n` por moneda, `any_range_ge2_rate` y
  `any_range_ge2_8h_rate` (alguna de las cuatro) y, en Dubái, `nota` (sin calendario de festivos: cuenta los laborables).
- `chain` (`null` hasta que se calcula): con las aperturas de las ventanas del laboratorio (`backtest_aperturas.build_openings`),
  las cuatro monedas juntas. `after_up_rate`: tras una apertura que deja la moneda en positivo a las 4 h, proporción de
  veces que sube en la siguiente que empieza cuando ya se sabe ese resultado (≥ 4 h después: sin mirar el futuro);
  `after_down_rate`, tras una en negativo (o nula); `base_rate`, de todas; `n` casos. Extras: `n_up`, `n_down`, `parcial`
  (con lo que llevaba la anterior al abrir la siguiente) y `tal_cual` (la siguiente sin más: se solapa y sale ~60 %, un
  artefacto: São Paulo y Nueva York abren con entre 30 y 90 min de diferencia según la época del año). **No predice**:
  `nota` lo dice con las cifras.
- Extras de primer nivel: `total` (la historia de todas las aperturas de las ventanas del laboratorio, con la misma forma
  que `hist`) y `hist_generated` (ms del cálculo). La historia se guarda en `BACKTEST_DIR/reaccion_aperturas.json`
  (`version` 2 desde la nota del solape corregida; `generated`, `desde`, `hasta`, `markets`, `total`, `chain`) y se
  reutiliza si tiene menos de 24 h y es de esa versión.
- Sin el módulo, `GET /api/cripto_aperturas` da el último estado del bus o `{"type": "cripto_aperturas", "estado": "apagado",
  "ts": null, "coins": […], "markets": {}, "chain": null}`; antes de la primera ronda, `"estado": "arrancando"`.

### 2.15 `noticias_fiables` (grupos de noticias que aciertan ≥ 60 % de forma constante)

Estado (no evento) de `bot/fiables.py`: al arrancar (el último guardado, al momento, y luego un cálculo) y cada hora, en
un hilo con su propia conexión de lectura, con lo que midió el aprendizaje (`news_outcomes`). Solo mide: no opera.
Versión 2 (28-10-2026, tras la revisión estadística): azar que conserva la hora, noticias distintas y puerta para operar.
```json
{"type": "noticias_fiables", "ts": 0, "version": 2, "desde": "2026-10-25", "hasta": "2026-10-27", "dias": 3,
 "dias_lista": ["2026-10-25", "2026-10-26", "2026-10-27"], "ventana": {"desde": "2026-09-28", "hasta": "2026-10-27"},
 "horas_por_dia": [{"dia": "2026-10-25", "horas": 2}], "unidades": 6597, "historias": 1763, "permutaciones": 500,
 "firma_datos": "…",
 "criterios": {"n_min": 30, "dias_min": 3, "n_dia": 5, "acierto_min": 0.6, "wilson90_min": 0.5, "dia_min": 0.5, "z": 1.645,
               "horizonte_min": 240, "ventana_dias": 30, "permutaciones": 500, "semanas_desde_dias": 14, "unidad": "…",
               "ajuste": "…", "azar": "…", "texto": "…"},
 "puerta": {"p_azar_max": 0.05, "historias_min": 30, "wilson_bloques_min": 0.5, "bloque_min": 30, "bruto_min": 0.5,
            "horquilla": {"stop_pct": 1.5, "objetivo_pct": 4.5, "horas": 24}, "horquilla_historias_min": 30,
            "adelante_historias_min": 30, "adelante_acierto_min": 0.6, "adelante_bruto_min": 0.5, "texto": "…"},
 "probados": 1649, "con_muestra": 56, "pasan": 6, "operan": 0, "duplicados": 348,
 "por_azar": 3.39, "por_azar_p95": 6, "por_azar_max": 9, "p_azar": 0.1118,
 "azar_bloques": {"media": 3.79, "p95": 7, "max": 10, "p": 0.1856, "bloque_min": 60},
 "familias": {"medio": {"probados": 590, "con_muestra": 19, "pasan": 1, "por_azar": 0.97, "por_azar_bloques": 1.4},
              "fuente": {"…": "…", "duplicados": 348}, "tipo_sentido": {}, "medio_sentido": {}},
 "grupos": [{"clave": "medio:rbc.ru", "tipo": "medio|fuente|tipo_sentido|medio_sentido", "nombre": "rbc.ru", "sentido": null,
             "n": 36, "n_historias": 9, "dias": 3, "dias_validos": 3, "acierto": 0.7222, "acierto_bruto": 0.5,
             "acierto_1h": 0.61, "n_1h": 36, "ic90_bajo": 0.5872, "bloques": 7, "ic90_bajo_bloques": 0.4086,
             "base_sentido": 0.5506, "horquilla_R": 0.216, "n_horquilla": 7,
             "vivo": {"n": 32, "n_historias": 8, "acierto": 0.78, "acierto_bruto": 0.56},
             "adelante": {"desde": null, "n": 0, "n_historias": 0, "acierto": null, "acierto_bruto": null},
             "por_dia": [{"dia": "2026-10-25", "n": 12, "n_historias": 3, "acierto": 0.75}],
             "estado": "fiable|en_prueba|no_fiable", "opera": false, "no_opera": ["9 noticias distintas (hacen falta 30)"],
             "outlet": "rbc.ru"}],
 "nota": "6 de 56 grupos con la muestra mínima pasan … por azar 3,4 (p = 0,112) y 3,8 dentro de cada hora (p = 0,186) …"}
```
- **Criterios fijados de antemano** (no se cambian mirando el resultado). Unidad: una historia×moneda (la fila del primer
  medio: rol `primera`, si no la de menor `orden`), con `dir` ≠ 0 y `acc240` medido (4 h desde el primer cierre tras el
  triaje: así cuentan también las que llegan tarde). Acierto = `dir × (acc240 − deriva) > 0`, deriva = media de `acc240`
  de TODAS las filas medidas de esa moneda y día UTC. `acierto_bruto` sin restar la deriva; `acierto_1h` con `acc60` y su
  deriva (`n_1h` filas). «fiable»: `n` ≥ 30 historias×moneda; ≥ 3 días con ≥ 5 (`dias_validos`); acierto ≥ 60 %; cota
  inferior de Wilson al 90 % (`ic90_bajo`, z = 1,645) ≥ 50 %; y acierto ≥ 50 % en CADA uno de esos días (desde 14 días
  con datos, en cada semana UTC de lunes a domingo con ≥ 5: por días, un grupo que acierte de verdad el 60 % casi nunca
  pasaría 30 días seguidos). `en_prueba`: acierta ≥ 60 % pero aún sin la muestra; `no_fiable`: el resto. Proporciones de
  0 a 1.
- **`n` son historias×moneda, no noticias:** una noticia que vale para las cuatro monedas son 4 unidades casi iguales.
  `n_historias` (en el grupo y en cada `por_dia`) cuenta las noticias distintas; `historias`, las del cálculo.
- **Sin mirar el futuro:** solo días UTC completos y anteriores al cálculo (un día cuenta cuando han pasado sus 4 h de
  medida y 2 h de margen: el de ayer entra a las 6:00 UTC); como mucho los 30 últimos (`BOT_FIABLES_DIAS`). `desde`,
  `hasta` y `dias` son los días con datos; `ventana`, la de la consulta; `horas_por_dia`, las horas UTC con noticias
  medidas de cada día (un «día» de 2 h es una sesión, no una jornada).
- **Grupos:** `medio` (`outlet`, con `outlet`), `fuente` (feed, con `fuente`; `nombre` = el del feed), `tipo_sentido`
  (`tipo_noticia`) y `medio_sentido` (`outlet`); `sentido` 1 o −1 en los de sentido y null en los demás. Claves:
  `medio:<medio>`, `fuente:<feed>`, `tipo_sentido:<tipo>:<±1>`, `medio_sentido:<medio>:<±1>`. Un feed que es el único de
  su medio, si el medio solo tiene ese feed, es el mismo grupo: no se prueba (`duplicados`, también en
  `familias.fuente.duplicados`). `grupos` = todos los fiables (por `ic90_bajo`) y los 60 mejores del resto por acierto
  con n ≥ 10. `probados` = grupos con alguna historia y `con_muestra` = los que tienen la muestra mínima (el denominador
  que usa la `nota`).
- **Azar** (`permutaciones` ≥ 50, por defecto 500, semilla fija; `BOT_FIABLES_PERM`), dos cuentas con el mismo criterio:
  `por_azar`/`por_azar_p95`/`por_azar_max`/`p_azar`, la fijada de antemano (dirección barajada dentro de cada
  moneda×día), y `azar_bloques` {`media`, `p95`, `max`, `p`, `bloque_min` 60}: el sentido de historias ENTERAS barajado
  dentro de cada hora (conserva que las cuatro monedas de una noticia van juntas y que los aciertos se agrupan en el
  tiempo; la de moneda×día rompe las dos cosas y sale demasiado estrecha). p = (1 + veces que el azar llega a `pasan`) /
  (1 + permutaciones). La `nota` se basa en `azar_bloques.p`.
- **Puerta para operar** (`puerta`; fijada el 28-10-2026 tras la revisión, antes de tener más datos). Un grupo «fiable»
  solo se usa para entrar o vetar (`opera: true`, `operan` en el mensaje) si además: `azar_bloques.p` < 0,05;
  `n_historias` ≥ 30 y `ic90_bajo_bloques` ≥ 50 % (Wilson con un caso por bloque de 30 min, `bloques`: el acierto de un
  bloque es la parte de sus unidades que aciertan); `acierto_bruto` ≥ 50 % y `horquilla_R` > 0 en ≥ 30 noticias medidas a
  24 h (`n_horquilla`; la horquilla del usuario, stop 1,5 % y objetivo 4,5 % a 24 h, aproximada desde la captura con
  `max_sube`, `max_baja` y `r1440`: −1 si tocó el stop, también si tocó los dos; +3 si tocó el objetivo; si no, el cierre
  a 24 h; sin costes); y `adelante` (desde el primer día en que pasó el criterio con solo los días anteriores, con las
  noticias que el bot usaría en vivo, `senal` 1 o 2) con ≥ 30 noticias distintas, acierto ≥ 60 % y bruto ≥ 50 %.
  `no_opera` dice lo que le falta (vacía si opera). `vivo` (exploración): el mismo grupo solo con `senal` 1 o 2 en todos
  los días. Desde la versión 3 (28-10-2026) la 2 (una macro que la cripto aún no confirmó) cuenta como la 1, como en el
  aprendizaje: con solo las confirmadas se miraría el futuro, porque «confirmada» quiere decir que la cripto ya se movió
  a su favor en los 60 min que también mide `acc240` (antes, `senal` = 1). `base_sentido` (información): lo que acertaban todas las noticias del mismo sentido en esas monedas y días.
- **`fiable_para(medio, feed, tipo, dir)`** devuelve solo grupos con `opera: true`; un cálculo sin ese campo (de antes de
  la versión 2) no pone ninguno. `hay_fiables()` = hay alguno que opera.
- Mismos datos de entrada (`firma_datos`: días, filas medidas y sus sumas, criterios, puerta, versión y permutaciones) =
  mismo resultado: no se recalcula ni se guarda otro registro igual. Cada cálculo nuevo se AÑADE a la tabla
  `noticias_fiables` (4).
- `GET /api/noticias_fiables` devuelve el último (404 si aún no hay ninguno); va en el estado inicial del WebSocket.
  `BOT_FIABLES=0` lo apaga (y entonces no hay grupos fiables: ni veto ni entrada por noticia).

### 2.16 Entrada por noticia fiable confirmada (eventos `entrada_noticia`)

`bot/entrada_noticias.py` (solo paper; `BOT_ENTRADA_NOTICIAS=0` la apaga). Recibe captadas, triadas y reflejos por el gancho
del bus. Cuando se tría una noticia con dirección, importancia ≥ 4 y fuente no silenciada de un grupo FIABLE que pasa la
puerta para operar (`fiables.fiable_para`, 2.15; sin ninguno no hace nada ni publica nada, y las triadas esperan en la
cola hasta que ya no se puedan confirmar a tiempo):
1. Monedas (`fiables.monedas_de`): las que nombra el triaje (si no es de cripto, solo cuando la cripto la confirmó) o las
   cuatro si es una macro confirmada por el reflejo (2.6). Mientras falta el reflejo, espera sin publicar; si no se
   confirma, `vetada` (una vez, `moneda: null`); sin moneda, `vetada`.
2. `esperando`: espera a que el precio de la moneda se mueva a su favor al menos `umbral_pct` (`reflejo.umbral_de` de esa
   moneda: el mayor de 0,5 % y 2σ de 60 min de las 24 h anteriores; sin σ porque las velas aún no llegan 24 h atrás,
   0,5 % provisional y se recalcula en cada ronda hasta tenerla) en los 60 min siguientes a `ta` (primer cierre de
   1 min tras el triaje; `expira` = `ta` + 60 min). Referencia: el cierre en `ta`; se mira cada cierre de 1 min (velas de
   la vigilancia y la caché). Sin confirmar a tiempo, `caducada`; confirmada pero vista más de 3 min tarde, `caducada`
   (no se persigue el precio).
3. Confirmada: entra en ese momento por el MISMO camino que el motor: evento `strategy` (story `nf-<noticia>-<moneda>`,
   `patch.noticia`) → `submit` del paper (riesgo → aprobación → ejecución). Ticket: horquilla pura desde el precio de
   OKX EU (≤ 15 s): `stopPct` 1,5, `tp1R` = `tp2R` = 3, `tp1Frac` 0, `beR`/`trailPct`/`trailStartR` null, `winEnd` =
   ahora + 24 h (`holdLabel` «tope de 24 h»), `maxAgeMs` 60 000, largos al contado y cortos en perpetuo, `risk`
   = `config.RISK_PER_TRADE_PCT`, `mode` «A favor de la noticia», `origen` `noticia_fiable`, `noticia` = la story, `tags`
   (2.6) y estrategia `noticia-fiable-largo|noticia-fiable-corto`. Si entra, `confirmada` con `paper_story`, `entrada`,
   `stop`, `objetivo`, `instrumento` y `riesgo_pct`.
4. `vetada` (con `motivo`) si: no hay cartera, el paper está detenido, ya hay una posición, orden pendiente o programada
   en esa moneda (una por moneda, de cualquier origen), hay `MAX_POSITIONS` operaciones abiertas o en curso, el día de
   Madrid ya tiene 6 entradas o 3 % de riesgo comprometido (COMPARTIDOS con el motor: `apertura.paper_day_entries` más
   lo que acaba de enviar), no hay precio reciente, el plan no cumple las reglas del usuario (stop ≥ 1,5 %, R:R ≥ 2,
   objetivo ≥ 3 % neto de costes y funding esperado, sin break-even ni stop dinámico; se comprueban aunque
   `BOT_USER_RULES=0`) o el control de riesgo del paper la veta.
- Al arrancar recupera las triadas, reflejos y eventos `entrada_noticia` de los últimos 75 min: lo final no se repite y lo
  que ya no se puede confirmar a tiempo no se reabre.

## 3. API de solo lectura (servidor en 127.0.0.1:8765)

| Ruta | Devuelve |
|---|---|
| `GET /health` | Salud del proceso: `phase`, `sources` (cazadores), `market {ok, err, prices}`, `trend {ok, err}`, `feeds` (2.1), `local_ai {queue, done, dropped}` (cola del triaje, triadas y sin triar por cola llena), `db {mb, wal_mb, copias, ultima_copia}` (tamaño de `bot.db` y su WAL y las copias diarias de `DATA_DIR/copias`, hallazgos n.º 39 y 51) y `clients`. La usa `scripts/vigilante-bot.ps1` |
| `GET /api/estado` | Resumen para la IA local (precios, flujo, libro, agenda, fuentes, IA local y cartera). Desde el 28-10-2026, en lugar de `bolsas` (la variación de cada índice) lleva `aperturas_cripto`: por bolsa, `{nombre, estado, apertura (Madrid), minutos_desde_apertura, medida ("desde la apertura"\|"a las 4 h"\|"última apertura, a las 4 h"), cripto {moneda: {pct, rango_pct}} \| null, media_pct}` del último `cripto_aperturas` |
| `GET /api/noticias?horas=&min=&bolsa=&limite=` | Las `limite` más recientes (60 por defecto, 500 como mucho; hallazgo n.º 11: con 60 fijas, `horas=48` devolvía solo el último minuto) de las `horas` últimas (3 por defecto, 48 como mucho) con importancia ≥ `min`. Noticias con su triaje. Cada una lleva además `medio`, `estado_fuente`, `peso_fuente`, `peso_medio`, `clase` (`cripto\|macro\|contexto`, del prefiltro) y `reflejo` (el último del evento `reflejo`, o `null`); `credibilidad_ajustada` = la del nivel y las confirmaciones × peso del medio × peso de la fuente (0 si está silenciada, y 0 si es macro o geopolítica sin reflejo confirmado en la cripto); `confirmaciones` no cuenta las de fuentes silenciadas |
| `GET /api/fuentes?region=&estado=` | Lista de feeds (2.1) con `estado` y `peso` de su ciclo de vida (2.12); `estado=` filtra |
| `GET /api/tendencia` | Último `trend` y `orderflow` |
| `GET /api/predicciones` | Último `predictions` |
| `GET /api/backtest` | Contenido de `BACKTEST_DIR/resultados.json` (3.1) |
| `GET /api/predicciones/evaluadas?n=50` | Las últimas n predicciones ya evaluadas (hasta 500), con su acierto |
| `GET /api/cartera` | La cartera del paper ahora (`portfolio`, 2.5) |
| `GET /api/fase2` | Estado de la fase 2: `{claude, estado ("ok"\|"nokey"\|"apagada"…), presupuesto {limite_usd, gastado_usd}, planes}` |
| `GET /api/backtest/aperturas` | `BACKTEST_DIR/aperturas.json`: el motor anterior (v2 «ruptura»), solo como referencia (404 sin archivo) |
| `GET /api/backtest/impulso` | `{impulso, lab}` (`bot/resumen_backtest.py`): `impulso` = `BACKTEST_DIR/impulso_7d.json` (lo escribe `controles/compara_impulso.py`), la variante de 7 días que opera el motor, con `fuera` y `dentro` = `{n, mean_R, win_rate, salidas, horas_media, largos, cortos, desde, hasta, total_R, max_dd_R, curva_R [[ms, R acumulado]] (≤ 151 puntos)}` y `nota`; `lab` = resumen de `laboratorio.json` (`summary`, `promising`, `robust`, `avance` = cuenta honesta en avance trimestral con 0,5 % de riesgo); `seis` = `BACKTEST_DIR/seis_diarias_resumen.json` (la réplica del motor con el preset «6 diarias»), con la misma forma que `impulso` más `nombre` y `params`. Lo que falte va como `null`; 404 si faltan los tres |
| `GET /api/noticias_fiables` | Último `noticias_fiables` (2.15); 404 si aún no hay ningún cálculo |
| `GET /api/resultados` | Operaciones paper y agregados (3.2) |
| `GET /api/vigilancia` | Último `vigilancia` (2.8) y `operaciones`: las operaciones virtuales que cuentan, de la más reciente a la más antigua (filas de la tabla `vigilancia`, hasta 500) |
| `GET /api/aperturas` | Último `openings` (2.9) |
| `GET /api/cripto_aperturas` | Último `cripto_aperturas` (2.14), o `{"estado": "apagado"}` sin el módulo |
| `GET /api/reflejo` | Reflejo de las macro desde el arranque (`bot/reflejo.py`): `{type: "reflejo", estado: "encendido\|apagado", pendientes, finales: {confirmada, contraria, sin_reflejo}, tasa_confirmada, azar_confirmada_pct}`; la tasa real frente a la del azar |
| `GET /api/aprendizaje` | Último `aprendizaje` (2.10) más `medios` (ranking de 60), `postmortems` (20), `cambios` (30 cambios de peso) y `noticias` `{mejores, peores}` (las que mejor y peor funcionaron a 4 h, 20 de cada) |
| `GET /api/aprendizaje/medios?orden=peso\|acierto\|casos\|ruido\|ops\|R&limit=&veredicto=&min_casos=&q=` | Ranking de medios del último cálculo |
| `GET /api/aprendizaje/medio?id=dominio` | Todo lo medido de un medio (404 sin datos) |
| `GET /api/aprendizaje/noticias?cuales=mejores\|peores&horizonte=15\|60\|240\|1440&dias=30&limit=20&solo_senal=1` | Noticias con titular, medio, rendimientos, `explicacion` y `operaciones` [{operacion, postmortem}] |
| `GET /api/aprendizaje/estadisticas?por=nivel\|region\|idioma\|tipo\|ventana\|fase\|rol\|moneda` | Acierto por esa dimensión |
| `GET /api/aprendizaje/postmortems?limit=&origen=paper\|vigilancia` | Post-mortems: `resumen` (sin IA), `texto` (IA local o null), `estado` (`pendiente\|hecho\|error`) y `hechos`. La IA local los escribe en baja prioridad, pero con la cola de triaje llena uno cada 10 min pasa por delante (hueco reservado, hallazgos n.º 15 y 4) |
| `GET /api/aprendizaje/cambios?limit=&medio=` | Cambios de peso de medios (`id, ts, outlet, antes, despues, tipo, motivo, cuerpo`) |
| `GET /api/evolucion` | Último `evolucion` (2.11) más `informe_semanal`: el último informe entero (`secciones` [{titulo, lineas}], `texto`, `cifras`) |
| `GET /api/evolucion/historial?tipo=a,b&sujeto=&origen=&desde=&hasta=&limite=` | `{cambios, tipos}`: la línea de tiempo de `evolution_log`, de la más reciente a la más antigua |
| `GET /api/evolucion/informes?semana=AAAA-Wss&limite=12` | `{informes}`: los informes semanales completos |
| `GET /api/evolucion/propuestas?estado=pendiente\|aplicada\|descartada\|caducada\|revertida&limite=` | `{propuestas}` |
| `GET /api/fuentes_vivas?region=&estado=&tipo=feed\|medio&limite=300` | Último `fuentes_vivas` (2.12) más `lista` (las fuentes filtradas: silenciadas y retiradas primero) y `candidatos` (50) |
| `GET /api/fuentes_vivas/lista?region=&estado=&tipo=&limite=` | Fuentes con `id, tipo, nombre, estado, peso, motivo, cambio, desde, manual, manual_hasta, origen, cifras, nota, sugerencia, acciones[]` y, los feeds, `region, tier, url, medio, estado_medio, tecnico` |
| `GET /api/fuentes_vivas/candidatos?limite=` | Medios candidatos a feed propio con `estado_texto`, `nota`, `rss`, `cifras` y `acciones` |
| `GET /api/fuentes_vivas/historial?id=&n=` | Cambios de estado con `deshacer {accion, id}` |

Sin el módulo (apagado o sin arrancar), `/api/aprendizaje`, `/api/evolucion` y `/api/fuentes_vivas` devuelven el último estado del bus o `{"estado": "apagado"}` (`"apagada"` la evolución), y sus subrutas dan 404. Un parámetro no válido da 400.

Las órdenes de la web al bot (aprobar, cerrar, cambiar el modo, detener, reanudar y cancelar; y sobre fuentes, pesos de medios y evolución) van por el WebSocket `/events` (ver 3.3).

### 3.1 Backtest (`BACKTEST_DIR/resultados.json`)
```json
{"generated": 0, "data": {"indices": {"^N225": ["2016-10-24", "2026-10-23", 2450]}, "coins": {"BTC": ["2017-08-17", "2026-10-23", 3350]}},
 "strategies": [{"id": "encadenamiento-tokio-madrid", "family": "encadenamiento|ventana_cripto|bolsa_a_cripto|noticias|flujo",
   "name": "Si Tokio cierra en positivo, Madrid cierra en positivo", "description": "…",
   "n": 1450, "hit_rate": 0.58, "base_rate": 0.53, "lift_pp": 5.0, "p_value": 0.001,
   "avg_ret_pct": 0.12, "total_ret_pct": 35.2, "win_rate": 0.55, "profit_factor": 1.3, "max_dd_pct": 9.1,
   "sharpe": 0.8, "trades_per_year": 145, "actionable": true,
   "oos": {"n": 430, "hit_rate": 0.57, "avg_ret_pct": 0.1},
   "verdict": "funciona|dudosa|solo_correlacion|no_funciona|pocos_datos", "verdict_why": "…",
   "equity": [[0, 100.0]], "params": {}}],
 "leadlag": {"markets": ["tokio", "…"], "p_up_given_up": [[0.0]], "p_down_given_down": [[0.0]], "base_up": [0.0], "n": [[0]], "actionable": [[true]]}}
```
- `actionable`: A cierra antes de que abra B, así que se puede operar con esa información.
- `oos`: el último 30 % de los datos, sin usar para elegir parámetros.
- `verdict`:
  - `funciona` exige `n ≥ 200`, `p_value < 0.05`, `lift_pp ≥ 2` y que el resultado fuera de muestra mantenga el signo con `oos.hit_rate > base_rate`.
  - `pocos_datos` si `n < 100`.

### 3.2 Resultados (`GET /api/resultados`)
```json
{"start_equity": 10000, "equity": 10012.3, "cuenta": null,
 "trades": [{"id": "t…", "story": "n…", "strategy": "…", "window": "Apertura Londres", "market": "londres", "coin": "SOL",
             "side": "long", "qty": 3.1, "entry": 104.9, "exit": 106.0, "sl_initial": 103.2, "trail_pct": 1.2,
             "opened": 0, "closed": 0, "reason": "Stop dinámico", "pnl": 3.4, "pnl_pct": 1.05, "R": 0.7, "fees": 0.65,
             "headline": "…", "news_score": {"importancia": 7, "credibilidad": 80},
             "instrument": "spot|perp", "funding": 0.0, "commissions": 0.0, "tags": {}}],
 "by_strategy": [{"strategy": "…", "n": 4, "win_rate": 0.5, "pnl": 12.0, "avg_R": 0.3}],
 "by_window": [{"window": "…", "n": 0, "win_rate": 0, "pnl": 0}], "by_coin": [{"coin": "BTC", "n": 0, "win_rate": 0, "pnl": 0}],
 "equity_curve": [[0, 10000.0]], "daily": [{"date": "2026-10-23", "pnl": 0, "pnl_pct": 0, "n": 0}]}
```

- `cuenta` (2.5). Una operación cerrada al reconstruir un hueco (2.5, «Huecos») lleva `hueco` `{desde, hasta, velas,
  factor, completo, fuente}`, su `closed` es la hora de la salida reconstruida (el evento `closed` sale al reconstruirla,
  con `result.closedAt` = esa hora) y `reason` termina en «con el bot sin gestionarla (a las HH:MM Madrid, reconstruido
  con velas de 1 min de Binance)»; una que pasó huecos sin salir lleva `huecos`.
- `instrument`, `funding` (USDC; + cobrado, − pagado) y `commissions` (las comisiones de entrada y salida). `fees` = `commissions` − `funding`: el funding entra en el resultado, en `pnl` y en R. Las operaciones del motor de aperturas llevan `tags` (2.6, con `origen: "motor_aperturas"`) y su estrategia es `impulso-<largo|corto>-<noticia-a-favor|noticia-en-contra|sin-noticia>` (también con «6 diarias»: la versión la dice `tags.estrategia_firma` y el preset `tags.preset`). Las de la entrada por noticia (2.16) llevan `tags.origen: "noticia_fiable"` y `tags.story` (la de la noticia), ventana «Noticia fiable» y estrategia `noticia-fiable-<largo|corto>`. El evento `closed` mantiene sus campos de siempre.

### 3.3 Órdenes de la web al bot (WebSocket `/events`)

Por el mismo WebSocket por el que recibe los eventos, la web envía mensajes de texto con un objeto JSON. Todas actúan solo sobre el paper (dinero ficticio). El bot no contesta a cada mensaje: la confirmación es la cartera (`portfolio`, 2.5), que se publica al momento tras cada orden, junto con los eventos de 2.6.

| Mensaje | Qué llama en `PaperBroker` | Qué se ve después |
|---|---|---|
| `{"type": "decision", "story": "n…", "ok": true}` | `decide(story, ok, "Aprobada por ti" o "Rechazada por ti")` | `humanok` (y la ejecución) o `humanko` |
| `{"type": "close", "story": "n…"}` | `close(story, "Cierre manual desde la web")` | `closed`. Sin precio actualizado no se cierra, y el agente `exec` dice por qué |
| `{"type": "mode", "mode": "auto\|veto\|manual"}` | `set_mode(mode)` | `portfolio.mode`. Al pasar a `manual`, lo que esperaba el plazo de veto pasa a esperar tu aprobación (nuevo `humanwait` con 10 min de plazo) y ya no se aprueba solo |
| `{"type": "halt"}` | `halt("Detenida desde la web")` | `portfolio.halted = true` |
| `{"type": "resume"}` | `resume("Reanudada desde la web")` | `portfolio.halted = false` |
| `{"type": "cancel", "story": "n…"}` | `cancel(story, "Cancelada por ti desde la web")` | `humanko` si esperaba tu aprobación, `blocked` si estaba programada |

Órdenes sobre lo que mide y aprende (no tocan el paper ni las reglas del usuario; se atienden aunque no haya paper):

| Mensaje | Qué llama | Qué se ve después |
|---|---|---|
| `{"type": "fuente", "accion": "restaurar\|silenciar\|retirar\|activar\|a_prueba\|anadir", "id": "<feed> o medio:<dominio>", "motivo": "…"}` | `FuentesVivas.accion` (`anadir`: `id` = dominio de un candidato verificado) | `fuentes_vivas` (2.12) y `orden_web` (2.13); el cambio queda en `evolution_log` con tu motivo |
| `{"type": "peso_medio", "medio": "dominio", "peso": 1.2, "motivo": "…"}` (`peso: null` vuelve al automático: botón «Volver al automático» del ranking de medios) | `Aprendizaje.fijar_peso` | `orden_web`; el cambio queda en `medios_peso_hist` y la evolución lo copia a `evolution_log` |
| `{"type": "revertir_peso", "id": 12}` | `Aprendizaje.revertir`: deja el medio como estaba justo antes (en automático si lo estaba y eso lo devuelve allí; si no, fijado a mano en el peso de antes); una fila `veredicto` no se revierte | `orden_web` |
| `{"type": "evolucion", "accion": "aplicar\|descartar\|revertir\|evaluar\|modo", "id": "p…\|v…", "motivo": "…", "modo": "proponer\|automatico"}` | `Evolucion.orden_web` (aplicar, revertir, evaluar, modo) o `Evolucion.atender` (descartar) | `evolucion.ultima_orden`; aplicar, revertir y el modo quedan en `evolution_log` por la evolución, y descartar como `propuesta_descartada` (origen `web`) |

- Las de pesos también se admiten como `{"cmd": "aprendizaje_peso", "outlet": …}` y `{"cmd": "aprendizaje_revertir", "id": …}`.
- `id` de una fuente: cadena de 1 a 300 caracteres; `medio`: de 1 a 200; `peso`: número de JSON (no booleano ni texto) o `null`; `id` de un cambio de peso: entero. Lo que no cumple se registra y se ignora.

Reglas de los mensajes:
- `ok` tiene que ser un booleano de JSON (`true` o `false`). La cadena `"false"`, un número o la falta del campo no aprueban ni rechazan nada: la orden se ignora y queda registrada.
- `story` tiene que ser una cadena de 1 a 128 caracteres. Si no lo es, la orden se ignora.
- Un `type` desconocido se registra y se ignora. Un JSON roto se ignora.
- La web solo envía `halt`, `resume` y `cancel` si `portfolio.caps` los incluye. Un bot que no los admite los registra y los ignora.

Parada manual (`halt`):
- No entran órdenes nuevas: `submit` da `riskko` con el motivo «Paper detenido por ti», que va como primer control de la lista.
- Lo que espera aprobación no se aprueba solo. Al vencer el veto se rechaza (`humanko`, «Paper detenido por ti: no se aprueba sola»). Una aprobación a mano tampoco ejecuta: da `humanok` y después `blocked` con «Paper detenido por ti».
- Las entradas programadas esperan, y entran si se reanuda antes de que caduquen. Si caducan con el paper detenido, `blocked` con «Paper detenido por ti: la entrada programada caduca sin ejecutarse».
- Las posiciones abiertas se siguen gestionando para no dejar riesgo sin control: stops, break-even, objetivos, cierre por tiempo y `close`.
- Se guarda en `paper_state` (clave `manual_halt`) y sigue tras un reinicio hasta `resume`.
- Los agentes `risk`, `human` y `exec` lo dicen en su `now` («Paper detenido por ti: …»).
- Es distinta de `limits.halted`, la pausa automática por la pérdida máxima del día, que acaba sola a medianoche de Madrid.

`cancel` no toca las posiciones abiertas: para salir de una está `close`.

Origen permitido (vale para todas las órdenes, también las de fuentes, pesos y evolución). Los WebSocket no tienen CORS, así que sin este control cualquier página abierta en el navegador podría mandar órdenes. El servidor rechaza con 403 la conexión a `/events` si no cumple esto:
- Con cabecera `Origin` (un navegador), solo la propia web del bot: `http://127.0.0.1:<PORT>`, `http://localhost:<PORT>`, `http://[::1]:<PORT>` y `http://<BOT_HOST>:<PORT>`.
- Para admitir otros orígenes, `BOT_WS_ORIGINS` con una lista separada por comas, por ejemplo `BOT_WS_ORIGINS=http://127.0.0.1:5173`. La barra final no cuenta.
- Sin cabecera `Origin` (un cliente que no es un navegador), solo se acepta desde este PC (127.0.0.1 o ::1).

## 4. Tablas SQLite nuevas (en `bot.db`)

- `feed_state(id TEXT PRIMARY KEY, etag TEXT, last_modified TEXT, interval_s REAL, last_ok INTEGER, fails INTEGER)` (feeds).
- `predictions(id TEXT, ts INTEGER, kind TEXT, target TEXT, when_utc INTEGER, horizon TEXT, prob_up REAL, expected_pct REAL, outcome_pct REAL, hit INTEGER, body TEXT, PRIMARY KEY(id, ts))` (predicciones).
- `trades(id TEXT PRIMARY KEY, story TEXT, opened INTEGER, closed INTEGER, body TEXT)` (paper trading).
- `vigilancia(id TEXT PRIMARY KEY, estrategia TEXT, apertura TEXT, moneda TEXT, lado TEXT, entrada_t INTEGER, entrada REAL, stop REAL, objetivo REAL, salida_t INTEGER, salida REAL, R REAL, pct REAL, motivo TEXT, cuerpo TEXT)` (vigilancia, 2.8). Una fila por estrategia, apertura y moneda (`id` = código de la estrategia, `<ventana>-<día>` y moneda): un reinicio no duplica nada. `salida_t`, `salida`, `R` y `pct` van a NULL mientras está abierta; una cerrada ya no cambia (salvo el funding de un corto aún sin liquidar del todo). `lado`: `largo|corto`; `motivo`: `objetivo|stop|tiempo|abierta|sin_datos` (`sin_datos`: abierta que ya no se puede resolver tras un apagón largo; no cuenta); `salida` es el precio de salida con deslizamiento. `cuerpo` (JSON): `apertura_ms`, `ventana`, `ventana_nombre`, `dia`, `instrumento` (`contado|perpetuo`), `sl_pct`, `r`, `dur`, `R_modelo`, `pct_modelo`, `comision_perp_pct`, `funding_pct`, `funding_completo`, `previa` y, abierta, `R_latente`, `pct_latente` y `datos_hasta`.
- `vigilancia_estado(clave TEXT PRIMARY KEY, valor TEXT)` (vigilancia): `ultimo_calculo`, `datos_hasta`, `vigiladas` y `aperturas` (las evaluadas; se reescriben solo si cambian). Sirven para recalcular lo que faltó tras un apagón.
- Aprendizaje (2.10; las crea y migra `bot/aprendizaje.py`):
  - `news_outcomes`: una fila por historia triada, moneda y medio (`story, coin, outlet`), con los hechos del triaje, la verificación y el impacto, la ventana de apertura, si entraría en la señal (`senal`: 1 la usaría el motor; 0 no; 2 la usaría por su triaje, pero es una macro que la cripto no ha confirmado y el motor no la usa; para el acierto, 2 cuenta como 1, porque contar solo las confirmadas inflaría el acierto por construcción; una macro de la clase nueva, captada sin monedas, se mide con las cuatro) y lo medido con velas de 1 min desde el primer cierre tras la captura: `previo`, `desde_pub`, `r15`, `r60`, `r240`, `r1440`, `max_sube`, `max_baja`, horquilla del usuario para largo y corto, sesión de apertura (`ap15`, `ap240`) y medida accionable (`acc60`, `acc240`). Se rellena por etapas (`proxima`, `estado`).
  - `news_stories` (titular, resumen y expediente de cada historia medida), `medios_conteo` (piezas por medio o feed y día: captadas, filtradas, triadas, archivadas, confirmaciones), `medios_info` y `medio_fuente` (catálogo de medios y qué feeds los traen), `mercado_base` (lo que habría acertado la tendencia del día por moneda y horizonte).
  - `aportaciones` (parte de cada operación paper cerrada que se debe a cada noticia y medio) y `postmortems(id, origen, ref, story, moneda, cerrada, R, hechos, resumen, texto, estado, intentos, modelo, creado, actualizado)`.
  - `medios_peso(outlet, peso, auto, manual, veredicto, casos, acierto, cuerpo, actualizado)` y `medios_peso_hist(id, ts, outlet, antes, despues, tipo auto|manual|automatico|revertir|veredicto, motivo, cuerpo)`; `aprendizaje_estado(clave, valor)`.
  - Lo de más de 400 días se poda una vez al día; `store.prune` no las toca.
- Evolución (2.11; `bot/evolucion.py`, que `store.prune` no toca):
  - `evolution_log(id, ts, tipo, sujeto, antes, despues, motivo, evidencia, origen, revierte)`: `antes`, `despues` y `evidencia` en JSON. Solo se añade: dos disparadores impiden DELETE y UPDATE.
  - `evolution_proposals(id, ts, tipo, sujeto, clave, estado pendiente|aplicada|descartada|caducada|revertida, cuerpo, actualizada, resuelta_ts, resuelta_por, nota)`: no se borra.
  - `evolution_state(clave, valor)`: `modo`, `ultima_evaluacion`, `ultimo_cambio`, `bloqueos`, `observacion` y el cursor de la copia del aprendizaje.
  - Archivo `DATA_DIR/estrategia_activa.json` (escritura atómica): `version, estrategia "impulso", fuente preset|evolucion|usuario, params, lab_id, lab_twin, twin_filtro, codigo, nombre, pausada, desde, actualizado, motivo, registro, propuesta, firma, solo_paper: true` y, si lo escribió `scripts/activar_estrategia.py` (fuente `usuario`, o `preset` al volver a E2), `preset` (`seis|impulso`) y `descripcion`. Lo lee el motor (2.9) y nunca vale si rompe las reglas del usuario. Un ajuste de la evolución sobre un preset quita `preset` y `descripcion` (ya no es el preset).
- Noticias fiables (2.15; `bot/fiables.py`, que `store.prune` no toca):
  - `noticias_fiables(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, json TEXT)`: un registro por cálculo nuevo, con el mensaje `noticias_fiables` entero en `json`. Solo se añade: dos disparadores impiden DELETE y UPDATE. Con los mismos datos de entrada (`firma_datos`) no se repite; al arrancar se carga el último.
- Fuentes vivas (2.12; `bot/fuentes_vivas.py`):
  - `source_status(id, estado, desde, motivo, tipo, manual, origen, previo, nombre, spec, cifras)`: manda sobre el registro `bot/fuentes/*.json` (un feed sin fila está activo); `id` = id del feed o `medio:<dominio>`.
  - `source_status_log(ts, id, antes, despues, tipo, motivo, cifras, auto, por)`, `source_health(id, horas, desde)`, `fv_noticias` (cada noticia captada con feed, medio, hora, latencia, veredicto, si se trió y si la confirmaron; 120 días) y `fv_candidatos` (medios candidatos con su RSS y su estado).
