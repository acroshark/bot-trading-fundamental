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
  - El resto de tipos se guardan como último estado y se reenvían a cada web que se conecta, salvo los de `bus.NO_STATE` (hoy `decision_apertura`, 2.17), que solo salen en el cable.
- Textos para el usuario siempre en español, con decimales con coma solo en los textos (los números del JSON van como números).
- Ninguna fuente de red puede tumbar el proceso:
  - Atrapa las excepciones por fuente.
  - Reintenta con espera creciente.
  - Registra el fallo con `logging`.
- Todo opera en **paper** (dinero ficticio con precios reales). Ningún texto puede dar a entender que hay dinero real.
- Las reglas del usuario (`config.USER_RULES`, «o gano más de 2 o pierdo 1»; desde el 28-10-2026 también «dejar correr»): stop ≥ 1,5 %; toda parte que gana —objetivo fijo, salida parcial o suelo del cerrojo— lo hace en ≥ 2R (`RULE_MIN_RR`) y deja ≥ 3 % neto de costes (`RULE_MIN_NET_PCT`); nada mueve el stop antes de ganar ese mínimo (sin break-even ni stop dinámico). Se permite el **cerrojo**: al llegar a +N R el stop sube a un suelo que cumple lo anterior y desde ahí puede seguir al precio, solo por encima del suelo (nunca baja). El paper apaga la parcial o el cerrojo que no cumplan y rechaza el ticket sin objetivo fijo ni cerrojo válido; `evolucion.cumple_reglas` rechaza los parámetros que las rompan. No las cambia ningún módulo de los que miden y aprenden (aprendizaje, fuentes vivas, evolución): solo deciden qué noticias cuentan, cuánto pesan y qué estrategia (que las cumpla) opera el motor.

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

# bot/market.py  (ya publica "market": {"prices": {coin: float}, "flow": {coin: {ls, funding, funding_h, oi, taker, prem, pctl, label, extreme}}, "spread"})
#   funding: última tasa de Binance USDT-M en % de SU periodo; funding_h: horas de ese periodo (GET /fapi/v1/fundingInfo cada
#   6 h; los símbolos que no lista liquidan cada 8 h). Sin respuesta de Binance no va funding_h y se supone 8 h
#   (market.funding_8h y paper.funding_8h escalan a % por 8 h). Es la tasa de Binance, no la de OKX X-Perps EU: aproximación.

# Módulos que miden y aprenden (todos solo paper; los arranca __main__ y un fallo en uno no para el resto)
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
# bot/entrada_noticias.py  EntradaNoticias(bus, store, submit, get_portfolio, bars=CacheBars(live).get)
#   observe(evento): gancho síncrono del bus → eventos «entrada_noticia» (2.16) y entradas en el paper («strategy» →
#   riesgo → aprobación → paper, como el motor). BOT_ENTRADA_NOTICIAS=0 la apaga
# bot/apertura.py       paper_day_entries(cartera, ahora) -> {story: riesgo %}: TODAS las entradas del paper del día de
#   Madrid; la usan el motor y la entrada por noticia (topes diarios compartidos). PRESETS {impulso, seis, seis2,
#   ruptura3, seis4r, seis4r3}; RupturaRun (la Ruptura V3, modelo «ruptura3», con las cuentas de bot/ruptura3.py),
#   preset_of(params), preset_info(params, activa), exit_text(params, corto), window_plan_text(open_ms, params),
#   impulse_plan(..., atr_h_pct) y simulate_plan(..., end_active_ms) con la parcial y el cerrojo (paper.exit_rules,
#   paper.lock_update, paper.rule_win_ok). OpeningEngine(..., decisiones=Decisiones): por qué entra o no (2.17)
# bot/ruptura3.py       Ruptura V3 sin red: regimen(cierres, funding) (crypto-regime-analyzer con las 4 monedas),
#   tendencia_diaria(cierres), tendencia_4h(velas_4h, open_ms), sentido(zona, d_dia, t4) -> (d, código, motivo),
#   funding_8h(filas, t), margen, stop_estructural y orden (de los disparos). La usan el motor y
#   scripts/replica_ruptura3.py (réplica de 3 años, operación a operación con la medida prerregistrada)
# scripts/replica_seis4r.py  réplica de 3 años del preset «6 diarias 4R» con el motor (ImpulseRun, impulse_plan y
#   simulate_plan), operación a operación con la variante F0 2 % · 4R · 7d de controles/salidas_motor.py; escribe
#   BACKTEST_DIR/seis4r_resumen.json (3 de la API) solo si coincide, y controles/replica_seis4r.json y .txt
# bot/decisiones.py     Decisiones(bus, store, clock=now_ms)  → mensaje «decision_apertura» y tabla decisiones_apertura (2.17)
#   add(ventana_id, coin, fase, decision, motivo_codigo, motivo, **columnas) -> fila | None (nunca lanza; la guarda un
#   hilo), ultima(ventana_id, coin), ventana(ventana_id), fases(ventana_id), de_story(story), consulta(desde,
#   ventana_id, coin, limite) (GET /api/decisiones, en un hilo), observe(evento): gancho síncrono del bus (riesgo, paper
#   y entrada por noticia), flush() (espera al primer intento de guardar; lo que falla se reintenta aparte), close()
#   (antes de store.close()). La tabla y su migración aditiva: store.migrate_decisiones (Store la llama)
# bot/predict.py        Predictor(bus, store, get_trend, get_orderflow, get_market, get_backtest, get_reaccion=None)
#   get_reaccion() → último «cripto_aperturas» (tasa base de cada ventana y reacción de la apertura anterior)
#   registrar(store, tipo, sujeto, antes, despues, motivo, evidencia, origen) y leer(...): historial evolution_log (4)
#   apuntar_cambios_codigo(store): CAMBIOS_CODIGO (cambios del código que alteran el paper), una sola vez al arrancar
#   leer_estrategia_activa(), params_motor(activa), firma_motor(params): la estrategia que opera el motor (2.9)
# bot/server.py         Services(..., velas=velas_de(live, CacheBars(live).get)): velas(moneda, desde, hasta) -> [t, o, h, l, c]
#   de 1 min: las de la vigilancia en memoria si empiezan en «desde» o antes (guarda semanas: no lee el disco); si no, la
#   caché de HIST_DIR/1m más la memoria (CacheBars, con su propia caché de 8 meses-moneda). La usa GET /api/velas (§3), que
#   las lee en un hilo, de una en una, sin pedir nada a la red. agrupar_velas(filas, paso) y velas_respuesta(...) son puras
#   Services(..., velas_okx=VelasOKX(session), real=RealBroker): velas públicas de OKX EU (por defecto en GET /api/velas)
#   y el modo real (GET /api/real, mandos «real» con el origen estricto origen_web; 3.3)
# bot/comisiones.py (29-10-2026) Perfiles de comisiones de OKX EU y el que está en uso (estado del proceso):
#   PERFILES {okx_eu_con_xperps (por defecto: contado 0,08 / 0,10 %, X-Perps 0,02 / 0,05 %, cortos),
#   okx_eu_sin_xperps (contado 0,20 / 0,35 %, sin cortos ni perpetuos)} y «cuenta» (la comisión real leída en solo
#   lectura por el conector real: manda mientras exista). activo(), elegir(perfil), tasas(inst) {perfil, creador,
#   tomador}, tasa(inst, tipo), hay_cortos(), coste_rt_pct(inst) (tomador a la entrada y a la salida + 2 pb por lado:
#   la regla del 3 % neto), vista() (la que llevan portfolio.comisiones y /api/resultados), observar(msg) (gancho del
#   bus: «cuenta_real» pone o quita el perfil «cuenta»). BOT_COMISIONES elige el de arranque
# bot/velas_okx.py (29-10-2026) VelasOKX(session): velas(moneda, desde, hasta, tf) y respuesta(moneda, desde, hasta, tf,
#   máximo) con las rutas PÚBLICAS de OKX EU (GET /api/v5/market/candles e history-candles, instId BTC-USDC…, sin
#   claves ni firma), caché en memoria y ≤ 10 peticiones cada 2 s; ErrorVelas si OKX no responde
# bot/ranking.py (29-10-2026) Ranking(bus, get_resultados=broker.results, lab=LabVivoLectura) → mensaje «ranking» (2.20)
#   Filas comunes de BACKTEST_DIR/ranking/*.json (una por archivo; las deja scripts/ranking_importar.py o cualquier
#   estudio nuevo), las tarjetas del laboratorio vivo (de_tarjeta, pasadas a jornadas) y lo de en vivo (paper por
#   tags.preset y la vigilancia). normalizar(fila), entra(fila), ordenar(filas), jornadas_entre(desde, hasta), holm(ps),
#   foto_vivo() (en el bucle), calcular(foto) y consulta(foto, origen, cumple, min_op_dia, limite, fragil, todas) (en un
#   hilo; GET /api/ranking), Ranking.firma(respuesta) (su ETag). Solo escribe DATA_DIR/ranking_estado.json (lo visto:
#   «nueva» y el puesto de antes). Una fila o una tarjeta rota se salta y se apunta en fuentes.<fuente>.errores
# bot/bus.py dumps_web(o): el JSON de los mensajes del bus, de las respuestas de la API (server._dumps) y de la foto
#   inicial del WebSocket, sin NaN ni Infinity (pasan a null; JSON.parse no los admite y, en la foto, uno solo haría
#   perder a la web todo su estado)
# bot/lab_vivo_lectura.py (29-10-2026) LabVivoLectura(bus): bot/lab_vivo/ va en un proceso aparte (docs/07); el servidor
#   solo lee sus archivos (DATA_DIR/lab_vivo o BOT_LAB_VIVO_DIR, caché por mtime) y nunca importa bot.lab_vivo ni abre
#   lab_vivo.db. vista(), hipotesis(estado, familia, limite), hipotesis_detalle(id), diario(dia), cola(), generador(limite),
#   resumen() (mensaje «lab_vivo», 2.19), tarjetas() (para el ranking) y mando("pausar"|"reanudar") (crea o borra
#   pausa_manual, su único mando; el servidor lo deja pasar solo con el origen estricto, 3.3). run(): cada 30 s mira
#   estado.json y publica «lab_vivo» si cambió. Sin Services.ranking ni Services.lab_vivo, el servidor crea los suyos
# bot/okx_real.py       RealBroker(bus, store, get_market, session, get_orderflow) (docs/06): construido siempre en
#   __main__; sin secretos/okx.env queda APAGADO y no hace ninguna petición privada. run() en el gather. Recibe los
#   tickets solo con BOT_BROKER=real (por defecto, paper: el motor y la entrada por noticia van al paper; la fase 2
#   siempre al paper). Publica «cuenta_real» (2.18), eventos «real_aviso» y el agente «real»

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
                "instrument": "spot|perp", "funding": 0.0, "tags": {},
                "tp1": 106.0, "tp1_frac": 0.5, "tp1_done": false, "realized": 0.0, "qty0": 3.1,
                "lock_r": 3.0, "floor_r": 2.0, "lock_trail_r": 2.0, "locked": false, "floor": 104.0,
                "hold_until": 0, "hold_label": "tope de 24 h", "hold_active": 0, "sl_kind": "inicial",
                "costes": {"perfil": "okx_eu_con_xperps", "entrada": 3.25, "salidas": 0.0, "comisiones": 3.25,
                           "funding": 0.0, "total": 3.25, "entrada_R": 0.065, "salidas_R": 0.0, "funding_R": 0.0,
                           "total_R": 0.065, "tomador": 0.10, "creador": 0.08, "salir_ahora": 3.27,
                           "salir_ahora_R": 0.065, "objetivo": 2.73}}],
 "pending": [{"story": "n…", "deadline": 0, "mode": "veto|manual", "coin": "SOL", "side": "long|short"}],
 "scheduled": [{"story": "n…", "at": 0, "expires": 0, "coin": "SOL", "side": "long|short"}],
 "closed": [ /* últimas 50 operaciones, formato de 3.2 */ ],
 "limits": {"risk_per_trade_pct": 0.5, "max_daily_loss_pct": 3, "max_positions": 4, "day_loss_pct": 0.4,
            "halted": false, "halt_until": null},
 "mode": "auto|veto|manual", "halted": false, "caps": ["halt", "cancel", "comisiones"],
 "comisiones": {"perfil": "okx_eu_con_xperps", "nombre": "OKX EU con cuenta X-Perps", "elegido": "okx_eu_con_xperps",
                "config": "okx_eu_con_xperps", "por_defecto": "okx_eu_con_xperps", "cuenta": false,
                "spot": {"creador": 0.08, "tomador": 0.10}, "perp": {"creador": 0.02, "tomador": 0.05}, "cortos": true,
                "texto": "contado 0,08 % creador / 0,10 % tomador; X-Perps 0,02 % / 0,05 %; cortos en X-Perps",
                "fuente": "…", "regla_coste_pct": {"spot": 0.24, "perp": 0.14}, "deslizamiento_bps": 2.0,
                "tipos": {"tomador": "…", "creador": "…"}, "aviso": "Sin cuenta X-Perps en OKX EU no hay cortos y el contado cuesta 0,35 % …",
                "perfiles": [{"id": "okx_eu_con_xperps", "nombre": "…", "texto": "…", "cortos": true}, {"id": "okx_eu_sin_xperps", "…": "…"}],
                "backtests": {"comision_pct": 0.10, "deslizamiento_bps": 2.0, "texto": "…"}}}
```
- **Comisiones (29-10-2026, `bot/comisiones.py`):** las del perfil en uso, no una cifra fija. `comisiones` dice cuál (`perfil`: `okx_eu_con_xperps`, por defecto; `okx_eu_sin_xperps`; o `cuenta`, la comisión real leída en solo lectura por el conector real, que manda mientras exista), sus tasas en % por lado (`spot` y `perp` `{creador, tomador}`; `perp: null` sin X-Perps), si hay cortos y el `aviso` (sin cuenta X-Perps no hay cortos y el contado cuesta 0,35 % como tomador). La entrada, el stop, el cierre por tiempo y el manual (a mercado) pagan la de **tomador**; el objetivo y la salida parcial (órdenes límite), la de **creador**. Cada posición guarda las tasas con que se abrió (`comision` en la tabla `trades`) y sale con ellas aunque cambie el perfil; las abiertas antes del 29-10-2026 (sin `comision`) siguen con `config.FEE_PCT` (0,10 %) en los dos lados. El perfil se elige con `BOT_COMISIONES` o desde la web (`{"type": "comisiones"}`, 3.3) y se guarda en `paper_state` (clave `comisiones`). Un `BOT_COMISIONES` desconocido (una errata) usa el perfil más caro, `okx_eu_sin_xperps`, y `comisiones.aviso_config` lo dice («BOT_COMISIONES=… no es un perfil conocido…: se usa el más caro…»; `null` si es válido), que la web enseña en rojo. El perfil `cuenta` solo sale de la cuenta de verdad (`cuenta_real` con `demo: false` y `modo` `solo_lectura` o `real`; la demo de OKX o el modo apagado lo quitan) y tiene cortos si la cuenta tiene X-Perps (`cuenta_real.xperps` no vacío), aunque el conector no pueda usarlos por `acctLv` o `posMode`; sin la comisión del perpetuo leída vale la última leída o la oficial con X-Perps (0,05 / 0,02 %). El agente `risk` lo dice en su línea («… · comisiones: OKX EU con cuenta X-Perps (contado 0,08 % creador / 0,10 % tomador; X-Perps 0,02 % / 0,05 %; cortos en X-Perps)»).
- `positions[].costes`: lo pagado (`entrada`, `salidas` = las parciales, `funding` + cobrado / − pagado, `total` = comisiones − funding), cada uno también en R (`*_R`, sobre `riskUsd`), las tasas de la posición (`tomador`, `creador`), lo que costaría salir ahora a mercado con lo que queda (`salir_ahora`, tomador, sin deslizamiento) y lo que costaría salir en el objetivo (`objetivo`, creador; null sin objetivo fijo). `closed[]` lleva `costes` y `bruto` (3.2).
- **Sin cortos:** con un perfil sin X-Perps (`okx_eu_sin_xperps`, o `cuenta` sin X-Perps) un corto o un perpetuo da `riskko` «Sin cuenta X-Perps: no hay cortos ni perpetuos con el perfil de comisiones «…»…» con `motivo_codigo: sin_cortos`, y el motor de aperturas ni lo envía (`entrada/no_entra`, `sin_cortos`, 2.17).
- **El perfil se vuelve a mirar al ejecutar:** lo que depende de él (los cortos y la regla del 3 % neto del objetivo o del suelo del cerrojo) se comprueba al enviar y otra vez al ejecutar una pendiente (veto o a mano) o una programada: si el perfil ha cambiado entre medias y ya no la admite, `blocked` con el mismo texto más «· el perfil de comisiones ha cambiado desde que se envió» y `motivo_codigo` `sin_cortos` o `reglas`.
- `positions[].costes.salir_ahora` es sin deslizamiento; la web le suma el deslizamiento previsto (2 pb + ½ diferencial) para enseñarlo con la misma base que «al stop».
- `mode`: modo de aprobación actual. Lo cambia la web con `{"type": "mode"}` (3.3).
- `halted`: parada manual del paper, puesta y quitada desde la web con `halt` y `resume` (3.3). No es lo mismo que `limits.halted`, que es la pausa automática por la pérdida máxima del día y dura hasta `limits.halt_until` (medianoche de Madrid).
- Quien envía tickets (fase 2 y motor de aperturas) mira antes `halted` y `limits.halted`: con cualquiera de los dos a `true`, `submit` da `riskko` seguro. Mientras dure, no llama al estratega de pago (la fase 2 usa sus reglas, con 0 tickets libres), no publica `strategy`, no envía nada y no lo cuenta como rechazo: solo espera y vuelve a mirar. La cartera sale al momento tras `halt` y `resume`.
- `caps`: mandos que este paper admite por el WebSocket además de `decision`, `close` y `mode`. La web solo ofrece detener, cancelar y elegir el perfil de comisiones si aparecen aquí (`halt`, `cancel`, `comisiones`).
- Se publica cada 2 s si ha cambiado algo y, al momento, tras cada orden de la web (`decision`, `close`, `mode`, `halt`, `resume` y `cancel`).
- Tamaño por riesgo: el nominal se elige para que la pérdida al saltar el stop inicial no pase de `risk_per_trade_pct` del capital, contando la comisión de entrada y la de salida (las dos de tomador del perfil en uso y del instrumento) y el deslizamiento previsto al salir (medio diferencial, o 1 pb sin dato, más 2 pb). El tope es el 40 % del capital. Un hueco que salte más allá del stop no se puede prever. Con más comisión (sin X-Perps, 0,35 %), menos nominal para el mismo riesgo.
- `riskUsd` sigue siendo el riesgo hasta el stop sin costes, que es la base de R (como en el backtest). Por eso una salida justo en el stop da algo peor que −1 R. El evento `filled` trae en `exec.stopLossUsd` la pérdida prevista en el stop con costes.
- `instrument`: `spot` (contado, por defecto) o `perp` (futuro perpetuo). En contado USDC no se puede ir corto: los cortos del motor de aperturas y de la fase 2 van como `perp`, y con las reglas del usuario un corto sin `instrument` también. Un perpetuo:
  - cobra o paga el **funding** en cada liquidación (00:00, 08:00 y 16:00 UTC), con la tasa de `market.flow[moneda].funding` escalada a 8 h con `funding_h` o, sin dato, la última conocida. Con tasa positiva los largos pagan y los cortos cobran;
  - `funding` (USDC, + cobrado, − pagado) es el acumulado de la posición; cada liquidación publica un `manage` con `exec.funding` y `exec.funding_n`;
  - cuenta su margen (la mitad del nominal) en la exposición, y el nominal total en perpetuos no pasa de 2 veces el capital (2:1);
  - paga la comisión de X-Perps del perfil en uso (con cuenta X-Perps, 0,05 % tomador y 0,02 % creador; hasta el 29-10-2026 pagaba la del contado, 0,10 %). Sin X-Perps no hay perpetuos.
- `tags`: las etiquetas del ticket (2.6), tal cual.
- **Gráfica de cada posición** (web, 29-10-2026): con `entry`, `opened`, `sl`, `sl_initial`, `tp2`, `tp1`/`tp1_frac`/`tp1_done`, `lock_r`/`floor_r`/`lock_trail_r`/`floor`/`locked`/`sl_kind`, `riskUsd`, `upnl` y `hold_until` la web dibuja, justo debajo de cada operación de «En posición» y en su expediente, el precio desde un poco antes de la entrada con un punto en la entrada, las líneas del stop, del objetivo (o de la parcial y el suelo del cerrojo) y el precio de ahora, y debajo la ganancia de ahora (% y R), lo que falta hasta el stop y el objetivo (%) y lo que daría cada uno (R total sobre el riesgo inicial y USDC, sin costes). Velas: `GET /api/velas` (§3; por defecto las de OKX EU del par, `fuente: "okx"`, y la nota «velas de OKX EU, BTC-USDC»; si OKX falla, las del bot con `fuente: "bot"` y `aviso`) de 1 min si la posición tiene menos de 3 h, de 5 min hasta 2 días y de 15 min después, desde 20 velas (1 min) o 24 velas antes de la entrada, repedidas cada minuto; encima, el precio de `market` que llega por el WebSocket. Sin la ruta (404) o sin velas, solo el precio en vivo desde que se abrió la web. Una posición sin `entry`, `opened` o stop válidos sale sin gráfica.
- **Parcial y cerrojo** («6 diarias v2», 28-10-2026): `tp1`/`tp1_frac`/`tp1_done` (la salida parcial y si ya se hizo; `realized` = lo cobrado en ella), `qty0` (cantidad inicial; `qty` es lo que queda: la posición «a medias» sigue siendo LA posición de esa moneda), `lock_r`/`floor_r`/`lock_trail_r` (activación, suelo y seguimiento en R; null sin cerrojo), `locked` (activado), `floor` (precio del suelo), `sl_kind` (`inicial|be|dinamico|cerrojo|seguimiento`), `hold_until` y `hold_label` (el cierre por tiempo de ahora: al activarse el cerrojo pasa a `hold_active`, p. ej. de 24 h a 7 días desde la entrada).

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
| `strategy`, `nostrategy` | `strat` | `strat{coin, side, mode, risk (% del capital), stopPct, trailPct, timing ("now"/"window"), win (texto), winAt (ms o null), entryType, tp1R, tp2R, tp1Frac?, beR?, trailStartR?, lockR?, lockFloorR?, lockTrailR?, maxAgeMs?, instrument? ("spot"/"perp"), tags?, winEnd?, holdLabel?, winEndActive?, holdActiveLabel?, valid, inval, why, llm, none? (bool)}`; el motor de aperturas añade `tags` también en el patch |
| `riskok`, `riskko` | `risk` | `risk{checks[{ok, t}], ok, why?, motivo_codigo?}` (`riskko`: `motivo_codigo` del catálogo de 2.17 para el primer control que falla, `paper.motivo_codigo`; `sin_cortos` si el perfil de comisiones no tiene X-Perps) |
| `humanwait`, `humanok`, `humanko` | `human` | `human{mode, deadline?, by?, at?, no?}` |
| `scheduled`, `order`, `filled`, `manage`, `blocked` | `exec` | `exec{scheduled?, sent?, px, slip (pb), ms, notional, sl, tp1?, tp2?, trail?, stopLossUsd?, blocked?, why?, motivo_codigo? (blocked, 2.17), instrument, funding?, funding_n?, lockR?, floorR?, lockTrailR?, floor?, holdUntil?, holdActive?, locked?, comision?, feeOpen?}` (un `manage` con `funding` es una liquidación de funding de un perpetuo; `filled` trae `comision {perfil, creador, tomador}`, las tasas con que se abre, y `feeOpen`, la comisión de entrada en USDC, que también dice su texto: «comisión de entrada 3,25 USDC (0,10 % tomador)»); `filled` lleva además `trade_id` en el patch (el `id` de la operación de 3.2). Dos `manage` llevan además, solo ellos, un campo propio en el patch: el que activa el cerrojo, `lock{at, lock_r, floor_r, trail_r, floor, hold_until}` (título «BTC: cerrojo a +2,0 R»), y el de la salida parcial, `partial{qty, px, at, pnl, fee, why, R (de la posición entera), tp1R, frac, qty_left, locked, sl, floor, hold_until}` (lo usa el registro de decisiones, 2.17). El seguimiento del cerrojo publica `manage` «BTC: stop de seguimiento a X» cada vez que sube 0,25 R; en la tabla `trades` la posición se guarda en cada subida, por pequeña que sea, y todas las abiertas al parar el bot: un reinicio no baja el stop |
| `closed` | `exec` | `result{coin, side, qty, entry, exit, why, pnl, pnl_pct, R, fees, opened, closedAt, riskUsd, notional, headline, strategy, window, costes, bruto}` (`costes` y `bruto` como en 3.2; el texto acaba en «· bruto +X USDC») y `trade_id` (el `id` de la operación); el evento lleva `tone: "good"` o `"bad"` |
| `real_aviso` | `real` | `aviso{…}` y `real{modo, demo}`: avisos del modo real (cambios de modo, parada hecha con `aviso.resultado {canceladas, cerradas, fallidas, errores}`, reconciliación, posiciones fuera del bot o apartadas, stop que no se pudo poner, orden en duda, cierre parcial). `aviso.sin_stop: true` marca una posición SIN STOP en OKX (aviso fuerte, en rojo); `false`, que ya lo tiene. `story` = `real` o la de la posición. Todo evento del modo real (también `riskok`, `filled`, `closed`…) lleva `patch.real` (2.18) |
| `reflejo` | `flow` | `reflejo{estado ("pendiente"\|"confirmada"\|"contraria"\|"sin_reflejo"), mov_pct, min, ref ("BTC"\|"media"), umbral_pct}` (ver abajo) |
| `entrada_noticia` | `strat` | `entrada_noticia{estado ("esperando"\|"confirmada"\|"caducada"\|"vetada"), motivo, motivo_codigo? (caducada y vetada con moneda: el catálogo de 2.17), moneda (o null: toda la noticia), dir (±1), lado, grupo{clave, tipo, nombre, sentido, n, n_historias, dias, acierto, acierto_bruto, ic90_bajo, opera}, ta, expira?, ts_triaje?, umbral_pct?, mov_pct?, min?, paper_story?, entrada?, stop?, objetivo?, instrumento?, riesgo_pct?}`; `story` = la de la noticia (ver 2.16) |

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
captura, la media de las cuatro monedas (o BTC si falta alguna); manda el primer toque. El umbral (`umbral_pct`) es el
mayor de 0,5 % y 2σ del movimiento de 60 min de esa misma referencia en las 24 h anteriores a la captura (sin 24 h de
velas, 0,5 %):
- `confirmada`: a favor de la dirección de la noticia, al menos el umbral, en los 60 min siguientes;
- `contraria`: al menos el umbral en contra;
- `sin_reflejo`: pasados 60 min, ninguna de las dos (y, sin velas en 3 h, `mov_pct: null`);
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
- `tags` (motor de aperturas): `lado`, `instrumento`, `noticia` (`a_favor|en_contra|sin_noticia`), `noticia_fuerza`, `noticia_fiable` (`a_favor|en_contra|ninguna`: noticias de un grupo fiable de las 6 h previas, 2.15) y `noticia_fiable_grupo` (nombre del grupo o null), `apertura_anterior`, `apertura_anterior_pct`, `btc`, `volatilidad_pct`, `impulso_pct`, `medida_min`, `rvol`, `estrategia_firma` (la versión de la estrategia que la abrió, `evolucion.firma_motor`), `origen: "motor_aperturas"` y `preset` (`impulso|seis|seis2|ruptura3|seis4r|null`: el preset de `apertura.preset_of`, null con cambios de la evolución; el ranking reparte por él las operaciones del paper, `ranking.PRESET_FILA`). El paper las copia en la posición y en la operación de 3.2.
- **Origen de las operaciones** (`origen` en el ticket y en sus `tags`, desde el 28-10-2026): `motor_aperturas` (motor de aperturas, story `a-<ventana>-<fecha>-<moneda>-<n>`, estrategia `impulso-…`) o `noticia_fiable` (entrada por noticia confirmada, 2.16: story `nf-<story de la noticia>-<moneda>`, estrategia `noticia-fiable-largo|noticia-fiable-corto`, ventana «Noticia fiable», `noticia` = la story de la noticia). Sin `origen`: la fase 2 o lo anterior al 28-10-2026.
- `tags` (entrada por noticia): `origen`, `story` (la de la noticia), `grupo` (clave), `grupo_nombre`, `grupo_tipo`, `acierto`, `ic90_bajo`, `umbral_pct`, `mov_pct`, `min_confirmacion`, `lado` e `instrumento`.

Salidas opcionales del ticket (`paper.exit_rules`). Un ticket sin estos campos se gestiona como antes: break-even a +1 R en la entrada, stop dinámico desde el principio y salida solo en `tp2R`.
- `beR`: R desde el que el stop pasa a break-even. `null` o ≤ 0 significa nunca. Si el campo está, el nivel cubre costes: entrada ± (coste de ida y vuelta + 2 pb), es decir, ±0,26 % con 0,10 % de comisión.
- `trailStartR`: R desde el que actúa el stop dinámico. `null` significa desde la entrada.
- `tp1R` + `tp1Frac`: salida parcial de esa fracción de la posición en tp1. 0 significa sin parcial.
- `tp2R`: objetivo final. `null` significa sin tope.
- `lockR` + `lockFloorR` (+ `lockTrailR`), el **cerrojo** (`paper.lock_update`): al llegar a +`lockR` R (el mejor precio desde la entrada) el stop sube al suelo, entrada ± `lockFloorR` R, y con `lockTrailR` sigue al mejor precio a esa distancia en R, solo por encima del suelo; nunca baja. Antes de `lockR` nada mueve el stop. Un `lockR` por debajo del suelo se activa en el suelo; sin `lockFloorR` no hay cerrojo.
- `winEndActive` + `holdActiveLabel` (con cerrojo): al activarse, el cierre por tiempo pasa de `winEnd` a `winEndActive` si es más tarde («6 diarias v2»: 24 h sin activar, 7 días desde la entrada activado).
- Con las reglas del usuario: `beR`, `trailPct` y `trailStartR` no se usan (nada mueve el stop antes del cerrojo); la parcial solo si `tp1R` ≥ 2R y deja ≥ 3 % neto (`paper.rule_win_ok`; si no, sin parcial); el cerrojo solo si su suelo cumple lo mismo (si no, sin cerrojo). Lo que la regla descarta se dice: el `filled` lo añade a su texto («salida parcial en 1,5 R descartada por tu regla…») y `exec.descartado [texto]`. El `tp2R` también tiene que dejar ≥ 3 % neto (`riskko` «Regla del usuario: el objetivo de 2,00 R con stop de 1,50 % deja 2,76 % neto…», `reglas`; la fase 2, `claude_agents.make_ticket`, ya lo sube lo justo). Sin `tp2R`, el ticket necesita un cerrojo válido: si no, `riskko` «Regla del usuario: sin objetivo fijo ni cerrojo…» o «…y el cerrojo no vale…» (`motivo_codigo: reglas`).
- Un número no finito (`Infinity`, `NaN`) en cualquier campo del ticket cuenta como ausente: un objetivo o cerrojo «infinito» no se cobraría nunca.
- `maxAgeMs` (solo `timing: "now"`): si la aprobación (veto o a mano) llega más tarde que esto desde el envío, el paper no la ejecuta (`blocked`). El motor de aperturas manda 60 000; sin el campo, como siempre.

Con parciales, el `closed` y la operación de 3.2 dan `qty` inicial, `exit` como precio medio de todas las salidas, y `pnl`, `fees` y `R` totales (R sobre el riesgo inicial). La operación de 3.2 añade `partials[]` (cada una con su `R` en R de la posición entera) y `exit_last`; con cerrojo, `lock_r`, `floor_r`, `lock_trail_r`, `locked` y `locked_at`. Motivos de cierre nuevos: «Stop del cerrojo» y «Stop de seguimiento» (con «tras salida parcial» si la hubo) y «Cierre por tiempo (7 días con el cerrojo)».

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
            "sl_pct": 3.0, "target_r": 3.0, "max_hold_h": 168, "tope_propio": false, "min_net_pct": 3.0, "long_instrument": "spot", "short_instrument": "perp", "…": "…",
            "tp1_r": null, "tp1_frac": 0.0, "lock_r": null, "floor_r": null, "trail_r": null, "trail_atr": null, "max_hold_active_h": null,
            "modelo": "impulso|ruptura3", "rp_max_apertura": 2, "rp_stop_max_pct": 3.0,
            "preset": "impulso|seis|seis2|ruptura3|seis4r|seis4r3|null", "nombre": "E2 · impulso|6 diarias|6 diarias v2|Ruptura V3: …|6 diarias 4R: …|<nombre de la evolución>|null", "descripcion": "…|null"},
 "salida": "stop 3 % y objetivo 3R (+9 %), sin tope (7 días de seguridad)", "salida_corta": "stop 3 % y objetivo 3R",
 "params_source": "preset_impulso|preset_impulso+entorno|manual|evolucion|usuario", "notice": null, "paused": false,
 "estrategia_cargada": {"firma": "3f2a…", "ts": 0, "pausada": false, "fuente": "evolucion", "registro": "v…", "preset": "seis"},
 "states": ["esperando_apertura", "midiendo_impulso", "esperando_entrada", "en_posicion", "terminada"],
 "day": {"date": "2026-10-27", "entries": 1, "risk_pct": 0.5, "max_entries": 4, "max_risk_pct": 2.0},
 "windows": [{"id": "europa-2026-10-27", "name": "Europa", "markets": ["frankfurt", "londres", "madrid"], "open_utc": 0,
              "state": "esperando_entrada", "why": "Se sigue el impulso de los 15 primeros min…",
              "hora_madrid": "09:00", "medida_hasta": 0, "entrada_ms": 0, "estrategia": "6 diarias v2",
              "hara": "medirá el impulso de los 15 primeros min de BTC, ETH, SOL y XRP y entrará a las 09:15 en el sentido del impulso; stop 2 %, la mitad en 3R y el resto corre",
              "regimen": {"zona": "RISK_ON", "puntos": 91.5, "peso_disponible": 0.85, "funding_8h": {"BTC": 0.0001}},
              "coins": {"BTC": {"state": "esperando_entrada", "step": "hora|retroceso|reanudacion|ruptura|null", "signal": 1,
                                "why": "…", "instrument": "spot|perp|null", "rvol": 3.0, "range": {"hi": 0, "lo": 0},
                                "news": {"dir": 0, "strength": 0.0, "why": "…", "label": "a_favor|en_contra|sin_noticia"},
                                "tags": {}, "levels": {"apertura": 0, "impulso_pct": 0.3, "maximo": 0, "minimo": 0, "extremo": 0,
                                                       "retroceso": null, "ruptura": null, "hora_entrada": 0, "rvol": 3.0,
                                                       "entrada": 0, "stop": 0, "objetivo": 0, "parcial": null, "suelo": null,
                                                       "movimiento_pct": 0.12, "minutos_medidos": 7},
                                "context": {"atr_pct": 0, "atr_h_pct": 0, "btc_dir": 1, "prev_ms": 0, "prev_react": 0.1},
                                "plan": {"stopPct": 3.0, "tp2R": 3.0, "rr": 3.0, "ok": true, "why": "…", "instrument": "spot",
                                         "gross_pct": 9.0, "cost_pct": 0.24, "funding_pct": 0.0, "net_pct": 8.76,
                                         "entry": 0, "stop": 0, "target": 0, "tp1R": 3.0, "tp1Frac": 0.0, "lockR": null,
                                         "lockFloorR": null, "lockTrailR": null, "partes": [{"que": "objetivo", "R": 3.0, "precio": 0,
                                         "frac": null, "bruto_pct": 9.0, "neto_pct": 8.76}], "parcial": null, "suelo": null},
                                "entries": 1, "results": [], "drivers": [], "strength": 0.0, "gate": null, "moving": null}}}]}
```
- **Reglas (modo impulso):** se mide el impulso de los primeros `measure_min` minutos de cada apertura; si pasa de ±`min_move_pct` %, se sigue con la entrada del laboratorio (`t:N`, `pb` o `brk`), con volumen relativo ≥ `rvol_min` y el filtro de BTC. Las noticias filtran (`news_mode`) y etiquetan. Salida (`apertura.impulse_plan`): horquilla pura de `sl_pct` % y `target_r` R sin tope de tiempo (`max_hold_h` = 7 días de seguridad) o, con cerrojo («6 diarias v2»), parcial y cerrojo (abajo); nunca break-even ni stop dinámico antes del cerrojo. Cada parte que gana (objetivo, parcial y suelo del cerrojo, `plan.partes`) tiene que ser ≥ 2R y dejar ≥ `min_net_pct` % neto de comisiones, deslizamiento y funding esperado (`plan.rr` y `plan.net_pct` son los de la peor). Una posición por moneda (también a medias); topes diarios de entradas y riesgo. Largos al contado, cortos en perpetuo.
- **Parcial y cerrojo (parámetros, 28-10-2026):** `tp1_r` + `tp1_frac` (salida parcial; `tp1_frac` 0 = sin parcial), `lock_r` + `floor_r` (cerrojo: activación y suelo en R), `trail_r` (seguimiento en R) o `trail_atr` (× ATR de 1 h al abrir, `context.atr_h_pct`; sin dato, un tercio del stop; no es el `trail_atr` de la ruptura), `max_hold_active_h` (tope desde la entrada con el cerrojo activado) y `target_r` null (sin objetivo final; solo con cerrojo). `apertura.impulse_params` los valida: sin cerrojo no hay seguimiento ni tope activado (con aviso); el cerrojo no se activa por debajo de su suelo; con las reglas del usuario, suelo y parcial suben a 2R. El ticket lleva entonces `tp1R`, `tp1Frac`, `lockR`, `lockFloorR`, `lockTrailR`, `winEndActive` y `holdActiveLabel` (2.6) y `tp2R` null.
- **«Programadas» y «Pasando controles» (web, 28-10-2026):** cada ventana trae `hora_madrid` (de la apertura), `medida_hasta` (fin de la medida, ms), `entrada_ms` (hora de entrada con `t:N`; null con `pb`/`brk`), `estrategia` (el nombre de la estrategia activa, o «impulso» si no es ningún preset) y `hara` (qué hará: `apertura.window_plan_text`). Las que aún no han abierto van en `esperando_apertura`; mientras mide, `coins[c].levels.movimiento_pct` (cierre de la última vela / apertura) y `minutos_medidos`; esperando la entrada, `esperando_entrada`. Arriba, `salida` y `salida_corta` describen la salida en uso. En posición, con la parcial hecha o el cerrojo activado, el `why` de la moneda lo dice («la mitad ya salió (+1,23 USDC) · cerrojo activado: stop en X (asegura +2,00 R), como mucho hasta el dd-mm HH:MM Madrid»).
- **Señal de noticias:** cada noticia triada pesa dirección × magnitud × credibilidad × decaimiento × `srcWeight`; la credibilidad sale multiplicada por el peso aprendido de su medio (`aprendizaje.source_weight`, 1 mientras no haya datos). Las de fuentes silenciadas o retiradas no cuentan (2.6).
- **Estrategia de la evolución:** en modo impulso, si existe `DATA_DIR/estrategia_activa.json` (lo escribe la evolución al aplicar o revertir) y cumple las reglas del usuario (`evolucion.leer_estrategia_activa`; si no, se ignora con un error en el registro), sus parámetros mandan sobre el preset y sobre `BOT_IMPULSO_PARAMS` (`params_source: evolucion`, o `usuario` si el archivo lo escribió `scripts/activar_estrategia.py`: `fuente: "usuario"`, como `estrategia_cargada.fuente`). Con `pausada`, el motor mide pero no abre entradas nuevas (`paused: true` y aviso en `notice`); las abiertas siguen en el paper. Se relee cuando cambia, cada minuto como mucho, y solo si ninguna apertura se está armando o está en su sesión de entrada: las aperturas aún no armadas se rehacen con los parámetros nuevos.
- `estrategia_cargada`: la versión que opera (`firma` = `evolucion.firma_motor` de sus parámetros, la misma que llevan los tickets en `tags.estrategia_firma`), desde cuándo (`ts`), si está pausada, de dónde salen los parámetros, el `registro` del historial que la aplicó y el `preset`. La evolución lo lee para saber si el motor ya opera la versión del archivo.
- **Presets (`apertura.PRESETS`, 28-10-2026):** `params.preset` dice a qué preset corresponden los parámetros en uso (`apertura.preset_of`: todos los de `PRESET_KEYS` iguales; con cualquier cambio de la evolución o de `BOT_IMPULSO_PARAMS`, `null`), y `params.nombre` / `params.descripcion` su nombre visible (sin preset, el `nombre` de `estrategia_activa.json`, o `null`).
  - `impulso` («E2 · impulso», el de siempre, por defecto): el de arriba.
  - `seis` («6 diarias»: «Impulso de los primeros 15 min sin filtros, stop 1,5 % y objetivo 3R, como mucho 24 h; veto de noticias fiables en contra»): `measure_min` 15, `min_move_pct` 0 (cualquier movimiento ≠ 0; un impulso de 0 exacto no se opera), `entry` `t:15` (al medir), `rvol_min` null, `vol_filter` `todos`, `btc_filter` `ninguno`, `news_mode` `fiables`, las 5 ventanas y las 4 monedas, `sl_pct` 1,5, `target_r` 3 (+4,5 %), `max_hold_h` 24, `min_net_pct` 3, `avg_hold_h` 13, largos al contado y cortos en perpetuo, `risk_pct` null (0,5 %), `max_day_entries` 6, `max_day_risk_pct` 3, `lab` null. Pierde en el backtest de 3 años (`seis_diarias_resumen.json`, 3 de la API): se mide en paper y solo se activa con `scripts/activar_estrategia.py --preset seis` (`--preset impulso` vuelve a E2), que escribe `estrategia_activa.json` y lo apunta en `evolution_log` (2.11).
  - `seis2` («6 diarias v2», 28-10-2026: «Impulso de los primeros 15 min sin filtros, stop 2 %; la mitad sale en 3R y el resto corre con un cerrojo (a +3R su stop sube a +2R y sigue al precio a 2R); 24 h sin activar y hasta 7 días activado; veto de noticias fiables en contra»): las entradas y los límites de `seis` con la salida «A»: `sl_pct` 2, `tp1_r` 3 y `tp1_frac` 0,5 (la mitad en +6 %, 5,76 % neto), `lock_r` 3, `floor_r` 2 (+4 %, 3,76 % neto), `trail_r` 2, `trail_atr` null, `target_r` null, `max_hold_h` 24, `max_hold_active_h` 168, `avg_hold_h` 16. También pierde en 3 años (`seis2_resumen.json`, docs/05 §21). `--preset seis2` la activa; `--preset seis` vuelve a «6 diarias».
  - `ruptura3` («Ruptura V3: rango de apertura con tendencia, volumen y régimen», 29-10-2026; docs/05 §24): `modelo` `ruptura3` (la moneda la sigue `apertura.RupturaRun` en vez de `ImpulseRun`), `measure_min` 15 (el rango de apertura), `entry` `brk` y `brk_tf` 5 (la ruptura al cierre de 5 min; con `modelo` `ruptura3` no se pueden cambiar), `entry_end_min` 240 (la ruptura vale hasta +240 min; `session_max_min` 240; con `modelo` `ruptura3`, `measure_min` tiene que ser un múltiplo de 5 y `entry_end_min` ≥ `measure_min` + 5, o vuelven a los del preset con una nota en `notice`; `rp_max_apertura` es None o un entero ≥ 1 de verdad: `true` o 2,5 no valen), `rvol_min` null, `vol_filter` `todos`, `btc_filter` `ninguno`, `news_mode` `corroboradas`, `sl_pct` 2 (el suelo del stop), `rp_stop_max_pct` 3, `rp_max_apertura` 2 y la salida, los límites y los instrumentos de `seis2`. Reglas fijas del diseño (`bot/ruptura3.py`): régimen cripto con las 4 monedas (largos solo en RISK_ON, cortos solo en RISK_OFF), tendencia diaria (cierre y SMA20 frente a SMA50) y de 4 h (cierre, EMA20 y EMA50) en ese sentido y ADX(14) de 4 h ≥ 25 con el DI a favor; disparo con la primera vela de 5 min que cierra fuera del rango más el margen (máx(2 pb, 5 % de la altura)), con volumen ≥ 1,5 × la media de las del rango y rango verdadero ≥ 1,5 × ATR(14) de 5 min; un cierre al otro lado antes la descarta; stop máx(`sl_pct`, el otro lado del rango + 0,10 %) y, si pasa de `rp_stop_max_pct`, no se entra. El ticket lleva `stopPct` = ese stop (R de la operación) y la salida A. `--preset ruptura3` la activa (backtest en `ruptura3_resumen.json`).
  - `seis4r` («6 diarias 4R: stop 2 %, objetivo 4R, hasta 7 días», 29-10-2026; docs/05 §25: «Impulso de los primeros 15 min sin filtros, entrada a +15 min, stop 2 % y objetivo 4R (+8 %), como mucho 7 días; horquilla pura, sin parcial ni cerrojo; veto de noticias fiables en contra»): las entradas y los límites de `seis` con `sl_pct` 2, `target_r` 4 (+8 %, 7,76 % neto al contado), `max_hold_h` 168 (el cierre a los 7 días sale con lo que haya: la excepción conocida), `avg_hold_h` 35 y sin parcial, cerrojo, break-even ni stop dinámico (`tp1_frac` 0, `lock_r` null). Era la n.º 1 del ranking el 29-10-2026 (`salidas_motor:F0 stop 2 % · objetivo 4R · tope 7d`); su réplica (`scripts/replica_seis4r.py`) da las mismas 2.319 operaciones: +0,12 R fuera de muestra y −0,04 R dentro, la mejor de 378 salidas elegida después de ver los datos (frágil). El ticket es la horquilla de siempre (`tp1R` = `tp2R` = 4, `winEnd` a +168 h), pero sus 7 días son su tope y no de seguridad (`tope_propio` true): `valid` «como mucho 7 días (hasta el …)», `holdLabel` «tope de 7 días» (el cierre del paper, «Cierre por tiempo (tope de 7 días)»), `salida` «stop 2 % y objetivo 4R (+8 %), como mucho 7 días» y el «por qué» de la moneda en posición acaba en «como mucho 7 días». `--preset seis4r` la activa (backtest en `seis4r_resumen.json`; el apunte del historial añade que es frágil, `activar_estrategia.AVISO`).
  - `seis4r3` («6 diarias 4R · stop 3 %: stop 3 %, objetivo 4R, hasta 7 días», 29-10-2026, el que opera la cartera paper desde las 23:59; docs/05 §26): `seis4r` con `sl_pct` 3 (objetivo +12 %) y `avg_hold_h` 59. Réplica: `scripts/replica_seis4r.py --preset seis4r3` → `BACKTEST_DIR/seis4r3_resumen.json` («seis4r3» en `/api/backtest/impulso`) y `controles/replica_seis4r3.json`/`.txt`, solo si coincide operación a operación con «F0 stop 3 % · objetivo 4R · tope 7d» de `salidas_motor.py`. `--preset seis4r3` la activa.
- **Ruptura V3 en vivo (`modelo: "ruptura3"`):** desde +1 min, antes que las monedas, el motor calcula una vez por ventana el régimen cripto (`ventana.regimen`: `zona`, `puntos`, `peso_disponible`, `funding_8h` por moneda) con los cierres diarios de las 4 monedas (Binance 1d, los ya cerrados) y la última liquidación de funding de cada una (`/fapi/v1/fundingRate` con `endTime` = la apertura; si falla, `HIST_DIR/funding`, que mantiene la vigilancia; una última liquidación de más de 16 h antes de abrir no vale, de ninguna de las dos, y queda en `bot.log`: ese componente queda sin dato y su peso se reparte). Mientras el régimen no está, ninguna moneda pide nada; si fallan los cierres, el régimen espera solo (10 s, 20 s, 40 s… hasta 5 min) y guarda lo que ya bajó. Luego, por moneda, su tendencia diaria y la de 4 h (1.000 velas de 4 h de Binance cerradas antes de abrir): `coins[c].context.rp = {zona, puntos, d_dia, t4 {d, adx, di, ext, cierre, ema20, ema50}, sentido, codigo, motivo}`. Las velas de 1 min se piden desde 80 min antes de la apertura (el ATR de 5 min). Los disparos de todas las monedas de una ronda se ordenan (`ruptura3.orden`: el que dispara antes y, en la misma vela, el de mayor ADX de 4 h) antes de entrar; al entrar se vuelve a pedir la señal de noticias (veto de `news_mode` `corroboradas`) y se aplica `rp_max_apertura` (cuentan las ya enviadas al paper o recuperadas de él; una moneda ocupada o vetada deja su hueco a la siguiente). La estrategia del paper es `impulso-ruptura3-<largo|corto>-<noticia>` (el prefijo `impulso-` es el de todas las del motor) y las etiquetas llevan `modelo`, `regimen`, `regimen_puntos`, `tendencia_diaria`, `tendencia_4h`, `adx_4h`, `stop_pct`, `stop_estructural_pct`, `volumen_x`, `rango_verdadero_x`, `min_tras_apertura`, `noticia_corroborada` y `noticia_corroborada_txt`. `coins[c].levels` añade `ruptura`, `margen`, `regimen`, `regimen_puntos`, `adx_4h`, `stop_pct` y `hasta_ms`; `step` es `ruptura` mientras espera. Los títulos dicen «Ruptura V3»; `hara`, «vigilará el rango de los 15 primeros min de … y entrará en la ruptura …». La versión (`evolucion.firma_motor`) incluye `modelo`, `brk_tf`, `rp_max_apertura` y `rp_stop_max_pct` solo con un modelo distinto de `impulso` (las firmas de antes no cambian).
- **Fallos de datos (29-10-2026, todos los modos):** si fallan el contexto o las velas de una moneda, solo esa moneda espera (10 s, 20 s, 40 s… hasta 5 min, por ventana) y las demás siguen en la misma ronda; antes, el fallo cortaba la ronda en esa moneda y la espera era de todo el motor. El régimen de la Ruptura V3 tiene su propia espera (arriba). Si la sesión acaba sin que llegara el contexto de una moneda, su fila es `medida/no_entra` `sin_contexto` (2.17), no `sin_velas`. Los disparos de una misma vela que llegan en rondas distintas (una moneda que se recupera tarde) no se reordenan por ADX: el que llega después entra si queda hueco en `rp_max_apertura` y dentro de su minuto y medio.
- **`news_mode: "fiables"`:** veta la entrada si en las `news_hours` (6) horas previas hay una noticia de un grupo FIABLE (2.15, `fiables.fiable_para` con su medio, feed, tipo y sentido) que se aplica a esa moneda (`fiables.monedas_de`: la nombra o es una macro confirmada por la cripto) y va en contra del impulso. Solo cuentan los grupos que pasan la puerta para operar (2.15): sin ellos no veta nada. La señal (`signal_from`) lleva entonces `fiables: [{story, dir, ts, grupo, nombre, acierto, titular}]` (el campo no sale si no hay ninguna) y las etiquetas, `noticia_fiable`.
- **`news_mode: "corroboradas"` (Ruptura V3, 29-10-2026):** veta la entrada si en las 6 h previas hay, en contra, una noticia de un grupo fiable (como `fiables`) o una macro o geopolítica que la cripto confirmó (`reflejo.macro_confirmada`: vale para las 4 monedas, sea del medio que sea). La señal lleva entonces también `macro: [{story, dir, ts, titular}]` (no sale si no hay ninguna; `datos.noticias.macro` en las filas de 2.17) y las etiquetas `noticia_corroborada` (`en_contra|a_favor|ninguna`) y `noticia_corroborada_txt`. Una noticia a favor no abre ni se exige: solo etiqueta.
- **Topes diarios compartidos (28-10-2026):** `day.entries` y `day.risk_pct` cuentan TODAS las entradas del paper del día de Madrid (`apertura.paper_day_entries`: posiciones y cerradas abiertas hoy, pendientes y programadas; también las de la entrada por noticia y las de la fase 2) más las que el motor acaba de enviar. Antes solo contaban las del motor (`a-…`). La entrada por noticia (2.16) cuenta lo mismo.
- Con `max_hold_h` < 168 (24 h en «6 diarias»), el ticket lleva `valid` «como mucho 24 h (hasta el …)» y `holdLabel` «tope de 24 h»; con 168, como siempre (`valid` «sin tope: como mucho 7 días (hasta el …)» y `holdLabel` «7 días de seguridad»), salvo con `tope_propio` (`seis4r`: los 7 días son el tope de la estrategia): `valid` «como mucho 7 días (hasta el …)» y `holdLabel` «tope de 7 días». `tope_propio` (false por defecto; true en `seis4r`) solo cambia esos textos (`hold_text`, `exit_text`, el ticket y el «por qué»): no entra en `preset_of` ni en `evolucion.firma_motor`. Con cerrojo y `max_hold_active_h` mayor, además `winEndActive` (entrada + `max_hold_active_h`), `holdActiveLabel` «7 días con el cerrojo» y `valid` «… sin activar el cerrojo; activado, hasta …».
- **Por qué entra o no (28-10-2026, 2.17):** en modo impulso, cada punto de decisión de cada apertura×moneda va a `decisiones_apertura`. Al reiniciar, el `why` y el estado de una moneda salen de su última fila: una decisión final o una entrada ya enviada al paper la terminan con su motivo (nunca se repite una entrada), y con la medida hecha y la entrada aún por llegar se vuelve a medir con las noticias de entonces (`datos.noticias` de la fila). Una ventana cuya sesión acabó con el bot parado: con filas, cada moneda sale de la suya (no «pasó con el bot parado»); sin filas y sin ningún evento guardado durante la sesión, `why` «La sesión de esta apertura pasó con el bot parado: no se siguió» y una fila `perdida` por moneda; sin filas pero con eventos (la siguió una versión que no las guardaba), `why` «Esta apertura la siguió el bot antes del último reinicio sin guardar su decisión (versión anterior): no se repite» y ninguna fila. Al cerrar la sesión sin medir (sin velas) el `why` es «Fin de la sesión sin medir el impulso: no llegaron las velas de 1 min» (con la Ruptura V3, «…sin medir el rango…»); si lo que nunca llegó fue el contexto de la moneda, «Fin de la sesión sin contexto: <no llegó el régimen cripto (los cierres diarios de Binance) | no llegó la tendencia diaria de BTC | no llegaron de Binance los datos de <moneda> para su contexto (velas de 1 h[, de 4 h] o de la apertura anterior)>; no se pudo decidir» (`sin_contexto`); sin entrar, «Fin de la sesión sin entrar».

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
 "reglas": {"stop_min_pct": 1.5, "rr_min": 2.0, "objetivo_neto_min_pct": 3.0, "break_even": false, "stop_dinamico": false,
            "parciales": "≥ 2R y ≥ 3 % neto", "cerrojo": "suelo ≥ 2R y ≥ 3 % neto; el seguimiento, solo por encima del suelo",
            "texto": "…"}, "criterios": {"texto": "…", "…": "…"},
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
{"type": "orden_web", "ts": 0, "area": "fuentes|aprendizaje|comisiones|real", "accion": "silenciar|…|peso|revertir|perfil|confirmar|…", "id": "medio:x.com", "ok": true, "why": null}
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
  noticias que el bot usaría en vivo, `senal` = 1) con ≥ 30 noticias distintas, acierto ≥ 60 % y bruto ≥ 50 %.
  `no_opera` dice lo que le falta (vacía si opera). `vivo` (exploración): el mismo grupo solo con `senal` = 1 en todos
  los días. `base_sentido` (información): lo que acertaban todas las noticias del mismo sentido en esas monedas y días.
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
- `caducada` y `vetada` con moneda llevan `motivo_codigo` (2.17): sin moverse lo bastante `impulso_bajo` (sin velas
  `sin_velas`), vista tarde `tarde`, moneda ocupada `moneda_ocupada`, `max_posiciones`, `tope_dia`, `riesgo_dia`,
  reglas `reglas`, el paper detenido por la pérdida del día `perdida_dia`, lo demás `otro`. Todo queda también en
  `decisiones_apertura` con la ventana `noticia` (2.17).

### 2.17 Por qué entra o no en cada apertura (mensaje `decision_apertura`, tabla `decisiones_apertura`)

`bot/decisiones.py` (28-10-2026; solo paper). El motor de aperturas en modo impulso y la entrada por noticia fiable apuntan
cada punto de decisión de cada apertura×moneda; los rechazos del riesgo y del paper y la apertura y el cierre de la
operación los apunta su gancho del bus (`observe`: `riskko`, `humanko`, `blocked`, `filled`, el `manage` de la salida
parcial —el que lleva `partial` en el patch, 2.6— y `closed`). Cada fila sale al
momento por el WebSocket y se guarda en la tabla (§4) en un hilo con su propia conexión: escribir no para el bucle
(medido: 0,05 ms de media y menos de 1 ms como máximo por fila, también con la base bloqueada por otro escritor). Si
guardar falla, queda en `bot.log` y en memoria (`GET /api/decisiones` la da igual) y se reintenta a 1 s, 2 s, 4 s… (como
mucho cada 5 min, unas 4 h en total); si el lote falla por una sola fila, se guardan las demás, y una `id` que ya está en
la base (otro bot escribiendo a la vez) se cambia por una libre. Lo que no se guarda tras reintentar, o al cerrar el bot,
queda en `bot.log` y no estará en la base. El motor sigue siempre.

```json
{"type": "decision_apertura", "ts": 0,
 "row": {"id": 17, "ts": 0, "ventana_id": "tokio-seul-2026-10-29", "ventana": "tokio-seul", "dia": "2026-10-29", "apertura_ms": 0,
         "coin": "BTC", "fase": "cierre", "decision": "cerrada", "motivo_codigo": "objetivo",
         "motivo": "Objetivo (3,0 R): +2,86 R (+125,00 USDC, dinero ficticio)", "impulso_pct": 0.3, "lado": "largo",
         "preset": "seis", "estrategia": "6 diarias", "trade_id": "t3f2a…", "story": "a-tokio-seul-2026-10-29-btc-1",
         "datos": {"R": 2.86, "resultado_usdc": 125.0, "resultado_pct": 4.3, "entrada": 100.3, "salida": 104.81}}}
```
- No va en el estado inicial del WebSocket (`bus.NO_STATE`): la web pide lo guardado a `GET /api/decisiones` (§3).
- `ventana_id`: la clave de la apertura del motor (`<ventana>-<día local de la bolsa>`, p. ej. `tokio-seul-2026-10-29`) o
  `noticia-<story de la noticia>` (entrada por noticia, `ventana: "noticia"`, `apertura_ms` null). `dia`: fecha de Madrid
  de la apertura (de la fila, en las de noticia). `impulso_pct`: el impulso medido (en las de noticia, lo que se movió el
  precio, `mov_pct`). `preset` y `estrategia`: los del motor (`params.preset` y `params.nombre`, 2.9) o
  «Entrada por noticia fiable». `story`: la del evento `strategy` (`a-…` o `nf-…`) desde que se envía al paper (antes,
  en las de noticia, la de la noticia); `trade_id`: el `id` de la operación del paper (3.2) desde que se abre.
- Una fila solo en puntos de decisión, y nunca igual (decisión, código, motivo y operación) a la última de la misma
  ventana×moneda×fase: un reinicio que vuelve a medir no duplica nada. Unas 100 al día. `ts` nunca es anterior al de la
  fila previa de la misma ventana×moneda. Solo se añade: nada se borra.
- `datos` (JSON, objeto en el mensaje y en la API; sin NaN):
  - `medida`: `apertura`, `precio` (cierre al medir), `rango {maximo, minimo}`, `medida_min`, `umbral_pct`, `entrada`,
    `rvol_min`, `filtro_volatilidad`, `volatilidad_pct`, `filtro_btc`, `btc_tendencia` (−1/0/1), `apertura_anterior_pct`,
    `filtro_noticias` y `noticias {dir, fuerza, motivo, factores [{factor, peso}], fiables [{story, dir, ts, grupo,
    nombre, acierto, titular}], etiqueta, fiable}` (las que pesaron; al reiniciar, `apertura.senal_de_fila` las vuelve a
    dar como señal).
  - `entrada`: `plan {entrada, stop, objetivo, stop_pct, objetivo_R, bruto_pct, neto_pct, coste_pct, funding_pct,
    instrumento, riesgo_pct, tope_h}` (con parcial o cerrojo, «6 diarias v2», además `parcial`, `parcial_R`,
    `parcial_frac`, `cerrojo_R`, `suelo`, `suelo_R`, `seguimiento_R`, `tope_activado_h` y `partes`; `objetivo` y
    `objetivo_R` null sin objetivo final), `limites {entradas, max_entradas, riesgo_pct, max_riesgo_pct}` (los del día de
    Madrid al decidir), `rvol` (volumen relativo medido al entrar) y `rvol_min` (null los dos sin ese filtro, como en
    «6 diarias»); con la Ruptura V3, en vez de `rvol` y `rvol_min`, `volumen_x` (volumen de la vela de 5 min que
    dispara frente a la media de las del rango) y `rango_verdadero_x` (su rango verdadero frente al ATR(14) de 5 min);
    `tarde`: `vela_ms`, `decision_ms`; sin volumen relativo: `rvol`, `rvol_min`. `medida/no_entra` `sin_contexto`:
    `error` (el último fallo de Binance, o null) e `intentos`. Si los datos de la medida
    no se pueden preparar, la fila va con `datos` null y la decisión es la misma.
  - `riesgo`: `controles_fallidos [texto]`. `paper` (abierta): `entrada`, `stop`, `objetivo`, `nominal_usdc`,
    `instrumento`, `deslizamiento_pb`, `riesgo_con_costes_usdc`; con parcial o cerrojo («6 diarias v2»), además el plan
    de salida que abrió el paper: `parcial` (precio), `parcial_frac`, `suelo` (precio), `cerrojo_R`, `suelo_R`,
    `seguimiento_R`, `hasta` (cierre por tiempo sin activar, ms) y `hasta_activado` (con el cerrojo activado, ms); solo
    los que tienen valor. `descartado [texto]` si la regla del usuario quitó la parcial o el cerrojo del ticket.
  - `cierre`: `R` (la web lo lee), `resultado_usdc`, `resultado_pct`, `entrada`, `salida`, `costes_usdc`, `motivo_paper`.
    En la `parcial`: `R` (lo que da esa parte, en R de la posición entera), `resultado_usdc`, `fraccion`, `precio`,
    `objetivo_R`, `comision_usdc`, `queda_qty`, `cerrojo` (si el resto ya va con el cerrojo), `stop`, `suelo` y `hasta`
    (el cierre por tiempo de lo que queda). La `cerrada` posterior trae el R de la operación entera (parcial incluida).
  - `perdida`: `arranque` (cuándo lo vio el bot), `fin_sesion`.

Fases y decisiones (la última fila de cada ventana×moneda es su estado):

| fase | decision | cuándo |
|---|---|---|
| `medida` | `esperando` | al abrir, mientras mide el impulso (una vez; no se apunta si el bot arranca con la medida ya pasada ni tras reiniciar si esa moneda ya tenía alguna fila en la apertura) |
| `medida` | `entra` / `no_entra` | al acabar la medida: el impulso pasa (y sus filtros) o no (`impulso_nulo`, `impulso_bajo`, `veto_noticia`, `sin_velas`, `otro`: volatilidad o BTC); al acabar la sesión sin que llegara el contexto de la moneda, `sin_contexto` (régimen, tendencias o velas de 1 h y 4 h de Binance) |
| `entrada` | `entra` | se envía al paper (evento `strategy`), antes de saber qué dicen el riesgo y el paper |
| `entrada` | `no_entra` | no se envía: `tarde`, `moneda_ocupada`, `tope_dia`, `riesgo_dia`, `reglas`, `sin_cortos` (un corto o un perpetuo con un perfil de comisiones sin X-Perps; `datos.comisiones` = el perfil), `perdida_dia` (paper parado por la pérdida del día), `otro` (sin cartera, paper detenido por ti, campeón pausado, sin volumen relativo, sin reanudación o ruptura a tiempo, fin de la sesión) |
| `medida` | `no_entra` | Ruptura V3 (`modelo` `ruptura3`), al abrir: `regimen_neutral` (régimen NEUTRAL o sin datos), `sin_tendencia` (la diaria o la de 4 h no van en el sentido del régimen), `adx_bajo` (ADX(14) de 4 h < 25 o DI en contra); al acabar el rango, `sin_velas` (faltan velas de 1 min del rango). `medida/entra` = rango hecho: se vigila la ruptura (`datos`: `modelo`, `rango {maximo, minimo, velas}`, `margen`, `ruptura`, `rango_min`, `ventana_min`, `regimen {zona, puntos}`, `tendencia_diaria`, `tendencia_4h {d, adx, di, ext}`, umbrales y `noticias`) |
| `entrada` | `no_entra` | Ruptura V3: `ruptura_contra` (una vela de 5 min cerró al otro lado del rango), `sin_ruptura`, `sin_volumen`, `sin_expansion` (al acabar la ventana sin disparo: nunca rompió a favor, rompió sin volumen ≥ 1,5 × el del rango, o con volumen pero sin rango verdadero ≥ 1,5 × ATR), `stop_lejos` (stop estructural > `rp_stop_max_pct`; `datos.stop_pct`, `stop_estructural_pct`, `volumen_x`, `rango_verdadero_x`), `tope_apertura` (ya hay `rp_max_apertura` entradas en esta apertura; `datos.limites.apertura` y `max_apertura`), `veto_noticia` (noticia corroborada en contra al disparar), `tarde`. La de `entrada/entra` añade en `datos.plan` `stop_estructural_pct`, `stop_max_pct` y `adx_4h`, y lleva `datos.volumen_x` y `rango_verdadero_x` en vez de `rvol` y `rvol_min` |
| `riesgo` | `rechazada` | `riskko` del paper (`exposicion`, `max_posiciones`, `perdida_dia`, `moneda_ocupada`, `reglas`, `sin_cortos`, `otro`) |
| `paper` | `abierta` | `filled`: abierta (con `trade_id`) |
| `paper` | `rechazada` | `humanko` (`aprobacion`: rechazada por ti, caducada) o `blocked` (`exposicion`, `max_posiciones`, `perdida_dia`, `aprobacion`: aprobada tarde o cancelada, `reglas` o `sin_cortos`: el perfil de comisiones cambió entre el envío y la ejecución y ya no la admite, `otro`); también si el paper nunca la abrió sin decir por qué |
| `cierre` | `parcial` | el `manage` de la salida parcial («6 diarias v2»: la mitad en 3R): `objetivo`, con `datos.R` de esa parte; la posición sigue abierta a medias (no es final: tras reiniciar, la moneda se da por hecha y no se repite la entrada) |
| `cierre` | `cerrada` | `closed`: `objetivo`, `stop` (de pérdida, break-even o dinámico), `cerrojo` (stop del cerrojo o de su seguimiento: sale ganando ≥ el suelo), `tiempo` u `otro` (manual), con `datos.R` de toda la operación |
| `perdida` | `perdida` | `bot_parado`: la sesión pasó con el bot parado (al arrancar, una vez por ventana×moneda; si no hay ningún evento guardado durante la sesión) o el bot se paró midiendo, antes de la hora de entrada o antes de apuntar esa moneda y volvió con la sesión acabada (en una apertura con filas de otras monedas: si son del motor, siempre; si solo hay filas del paper, como en el primer caso) |

Catálogo cerrado de `motivo_codigo` (un código fuera del catálogo se guarda como `otro`): `entra`, `impulso_nulo`,
`impulso_bajo`, `tarde`, `sin_velas`, `veto_noticia`, `tope_dia`, `riesgo_dia`, `perdida_dia`, `exposicion`,
`moneda_ocupada`, `max_posiciones`, `reglas`, `aprobacion`, `bot_parado`, `objetivo`, `stop`, `cerrojo`, `tiempo`,
`sin_cortos` (29-10-2026: sin cuenta X-Perps no hay cortos), `otro` y, de la Ruptura V3 (29-10-2026), `regimen_neutral`,
`sin_tendencia`, `adx_bajo`, `ruptura_contra`, `sin_ruptura`, `sin_volumen`, `sin_expansion`, `stop_lejos` y
`tope_apertura`, y `sin_contexto` (29-10-2026: la sesión acabó sin el contexto de la moneda).
Decisiones: `entra`, `no_entra`, `rechazada`, `perdida`, `abierta`, `parcial`, `cerrada`, `esperando` (`parcial` y el
código `cerrojo`, 28-10-2026).

Entrada por noticia (2.16): `esperando` → `medida/esperando`; su `strategy` → `entrada/entra`; `caducada` y `vetada` →
`entrada/no_entra` con su `motivo_codigo` (una `vetada` porque la rechazó el riesgo no repite la fila del riesgo);
`confirmada` no añade nada (ya está la de su `strategy`); sin moneda, ninguna fila. Las operaciones de antes de la
tabla (sin fila) se ligan por su story (`a-<ventana>-<fecha>-<moneda>-<n>` o `nf-<noticia>-<moneda>`); las de la fase 2
no van aquí: solo liga el paper una story `a-…` o `nf-…`, así que una operación de la fase 2 (story = la de la noticia)
no se cuela en la ventana `noticia-<story>` aunque la entrada por noticia haya apuntado esa noticia.

### 2.18 `cuenta_real` y el agente `real` (modo real con OKX EU, 29-10-2026)

El conector del modo real (`bot/okx_real.py`, docs/06) está enganchado pero **APAGADO**: sin `M:\BotFundamental-datos\secretos\okx.env` no hace ninguna petición privada y los tickets van al paper (`BOT_BROKER=paper`, por defecto). Con las claves del usuario lee su cuenta en solo lectura (saldo, comisiones, posiciones); opera solo con `OKX_DEMO=0`, `BOT_MODO_REAL=1`, su confirmación escrita en la web y `BOT_BROKER=real`.

- **`cuenta_real`** (último estado del bus, va en la foto inicial; sale al cambiar algo y tras cada mando): el contenido de `RealBroker.vista_real()`, lo mismo que `GET /api/real`: `modo` (`apagado|solo_lectura|demo|real`), `demo`, `motivos`, `etiqueta` (`apagado`, `solo lectura`, `demo de OKX` o `dinero real`), `listo`, `puede_abrir`, `por_que`, `bloqueo`, `avisos`, `parada` `{motivo, fecha, cerrar}`, `parando`, `halted`, `halt_until`, `aprobacion`, `archivos {carpeta, claves, confirmado, parar}` (si existen, nunca su contenido), `frase_confirmacion`, `cuenta {equity, usdc_disponible, monedas, acctLv, posMode, ip_atada, permisos, saldo_at, day_pnl, day_pnl_pct, inicio}` (USDC de verdad, nunca mezclados con los euros ficticios del paper), `comisiones {spot, perp}` (% de tomador de la cuenta, `null` sin leer) y `comisiones_creador {spot, perp}` (% de creador), `cortos {ok, por_que}` (si el conector puede abrir cortos: X-Perps, `acctLv` ≠ 1 y `net_mode`), `xperps` (monedas con X-Perps en la cuenta, aunque el conector no pueda usarlos; `null` sin leer: es lo que mira el perfil `cuenta` del paper), `instrumentos`, `limites` y `techos` (`max_usdc`, `perdida_dia_pct`, `max_posiciones`, `max_entradas_dia`), `entradas_hoy`, `posiciones` (con `partes`, `sin_stop`, `sin_stop_desde`), `sin_stop` (monedas SIN STOP en OKX o con la entrada sin confirmar: banda roja), `apartadas`, `externas`, `pendientes`, `cerradas`, `reconciliacion`, `ultimo_error`, `peticiones`; y `broker` (`paper|real`: a quién van los tickets) y `resumen` (una línea: «Modo real apagado: el bot opera en paper» mientras lo esté; «Modo real en solo lectura: el bot opera en paper (solo lee tu cuenta de OKX)»; con `BOT_BROKER=real`, «Operando en dinero real» o «Modo real sin operar: …»).
- La comisión de la cuenta que trae `cuenta_real` manda en el perfil de comisiones del paper (perfil `cuenta`, 2.5) mientras esté leída (`comisiones.observar`, gancho del bus), solo con la cuenta de verdad (`demo: false`, `modo` `solo_lectura` o `real`).
- **Agente `real`**: `{"type": "agent", "agent": "real", "patch": {"off": bool, "now": "…"}}`, una línea de estado; empieza por «¡SIN STOP EN OKX: …!» mientras alguna posición esté sin stop.
- **Eventos**: los mismos tipos que el paper (`riskok`/`riskko`, `humanwait`/`humanok`/`humanko`, `order`, `filled`, `manage`, `blocked`, `closed`) con `patch.real = {modo, demo}` y títulos con «(dinero real)», «(demo de OKX)» o «(solo lectura)»; código de veto nuevo «Tope diario de entradas…» → `tope_dia`. Y `real_aviso` (2.6).

### 2.19 `lab_vivo` (estado del laboratorio vivo, 29-10-2026)

El laboratorio vivo (`bot/lab_vivo/`, docs/07) va en un **proceso aparte** (`scripts/lab-vivo.ps1`). El bot solo lee
sus archivos de `DATA_DIR\lab_vivo` (`bot/lab_vivo_lectura.py`): nunca importa `bot.lab_vivo` ni abre `lab_vivo.db`,
que no está en `bot.db` (su esquema, en docs/07, apartado 12).

- **Es un estado** (último estado del bus, va en la foto inicial). Cada 30 s se mira `estado.json`; se publica si cambió
  su mtime, si el latido pasa a tener más de 3 min o si cambia la pausa manual, y al momento tras un mando (3.3):

```json
{"type": "lab_vivo", "ts": 0, "estado": "activo|pausa|en_espera|sin_datos|canario_fallido|error|arrancando|parado",
 "motivo": "…", "latido": 0, "aviso": "…", "proxima_pausa": {}, "contadores": {}, "embudo": [], "meta": {},
 "mejores": [], "en_avance": [], "listas": [], "canario": "ok", "recursos": {"procesos": 1, "memoria_libre_mb": 2048},
 "pausa_manual": false, "nota": "… (solo si está parado)"}
```

- Las listas van recortadas a 5 tarjetas (docs/07, 13.3), sin `curva` ni `checks`.
- `parado`: el latido de `estado.json` es de hace más de 3 min; el resto es lo último que hubo y `nota` dice desde qué
  hora.
- Sin `estado.json`: `{"type": "lab_vivo", "estado": "apagado", "nota": "El laboratorio vivo no está en marcha
  (scripts/lab-vivo.ps1)", "pausa_manual": false}`, una sola vez.
- Un archivo con tipos raros (listas que son objetos o números) no rompe nada: lo que no vale se queda fuera. Si el
  laboratorio deja NaN o Infinity en sus JSON, salen como `null` (bus.dumps_web).
- **Web:** pestaña «Laboratorio» (`#laboratorio`, docs/07 14.4). La demostración lleva datos de muestra incrustados en
  `<script type="application/json" id="labVivoData">`, marcados como tales; en directo el servidor los quita de la página
  (`server.BT_BLOCK`) y todo llega por las rutas de 3 y por este mensaje.

### 2.20 `ranking` (ranking de estrategias, 29-10-2026)

Lo pidió el usuario: todas las estrategias medidas, de más beneficio a menos, con las operaciones medias y los datos que
importan; las que vaya sacando el agente entran solas en su puesto, lleguen o no al 1 % diario, si no pierden.
`bot/ranking.py` junta tres fuentes, todas en solo lectura, que toleran faltar y se recargan solas por mtime:

1. `BACKTEST_DIR\ranking\*.json`: **una fila común por archivo** (también vale `{"filas": [...]}`). Los que empiezan
   por `_` no son filas (`_importado.json`, el resumen del importador). Para que entre un estudio nuevo basta con dejar
   su archivo. `scripts/ranking_importar.py` pasa los estudios ya hechos (idempotente; nombres `<estudio>--<id>-<firma>.json`).
2. El laboratorio vivo: las tarjetas de docs/07 13.3 (`mejores.json`, `estado.json` e `hipotesis\<id>.json`). Las
   cifras del laboratorio son por día natural: se pasan a jornadas con su mismo periodo fuera de muestra
   (× días naturales / jornadas, del corte al fin de la época).
3. En vivo: las operaciones del paper agrupadas por `tags.preset` (las de la Ruptura V3 también por `tags.modelo`; sin
   preset, `en_vivo:motor`, `en_vivo:noticia_fiable` o `en_vivo:paper_otras`) y la prueba en avance (vigilancia, 2.8: su
   id es el del laboratorio de 2.000; E2-7d es la fila de E2).

**Fila común:**

```json
{"id": "ruptura3", "nombre": "…", "familia": "…", "estudio": "ruptura3", "regla": "texto llano",
 "origen": "laboratorio_vivo|estudio|laboratorio_2000|curso|selectiva|salidas|en_vivo", "cumple_reglas": true,
 "perfil_costes": "backtest|laboratorio|okx_eu_con_xperps|okx_eu_sin_xperps|paper", "perfil_texto": "…",
 "perfil_corto": "costes del backtest", "fecha": "2026-10-29", "archivo": "ruptura3_resumen.json",
 "dentro": {"n": 159, "R": -0.033, "gana_pct": 37.1},
 "fuera": {"n": 118, "R": 0.4285, "gana_pct": 50.8, "p": null, "q": null, "jornadas": 306, "dias": null, "R_bruto": 0.5392},
 "op_dia": 0.386, "pct_dia": 0.0826, "R_dia": 0.1654, "op_dia_natural": 0.2738, "pct_dia_natural": null,
 "caida_max_pct": 4.17,
 "robustez": {"sin_5_mayores": 0.2468, "dentro_positivo": false, "mitades": [0.476, 0.3759],
              "corregida": {"tipo": "holm|bh|dsr|bonferroni|p|sin_p", "metodo": "Holm entre las 23 configuraciones del estudio (p frente a la base emparejada)",
                            "valor": 1.0, "pasa": false},
              "base_p": 0.0939, "sin_mejor_mes": null, "sin_mejor_semana": null, "desliz_x2": null},
 "en_vivo": {"fuente": "paper|vigilancia|lab_vivo", "virtual": false, "n": 1, "R": 0.5, "gana_pct": 100.0,
             "neto_usdc": 0.4, "bruto_usdc": 0.45, "desde": 0, "hasta": 0, "abiertas": 0, "avance": {}},
 "estado": "backtest|en_avance|en_paper|descartada_por_el_usuario", "fragil": true, "fragil_motivos": ["…"],
 "poca_muestra": false, "probadas": 23, "nota": "…", "perfiles": {"okx_eu_con_xperps": {"n": 118, "R": 0.47,
 "gana_pct": 50.8, "op_dia": 0.386, "pct_dia": 0.09}}, "igual_que": null,
 "posicion": 24, "posicion_antes": 23, "nueva": false, "entra_desde": null, "en_marcha": true,
 "archivo_ranking": "ruptura3--ruptura3-5c3a….json"}
```

- **Unidades.** R = resultado medio por operación, NETO de costes, con el perfil con que se calculó (`perfil_costes`,
  y en corto `perfil_corto`, que la web enseña en cada fila: el orden mezcla perfiles y no hay uno común): las réplicas
  del motor, las selectivas, el curso y las salidas, `backtest` (0,10 % por lado y 2 pb también en los perpetuos, con
  su funding, y la gestión del paper); el laboratorio de 2.000, `laboratorio` (5-20 pb más en los stops, todo con el
  modelo del contado y **sin funding**); el laboratorio vivo, `okx_eu_con_xperps` (más barato, con funding).
  `op_dia` = operaciones medias por **jornada con aperturas** (de lunes a viernes salvo el 1 de enero: 306 en el fuera de
  muestra de los estudios, `ranking.jornadas_entre`). `pct_dia` = % de la cuenta por jornada con 0,5 % de riesgo,
  fuera de muestra (R × 0,5 × `op_dia`, sin interés compuesto); `R_dia` = R × `op_dia`. Lo que falte en un archivo se
  calcula: `op_dia` con `fuera.n / fuera.jornadas`, `pct_dia` y `R_dia` con el R de fuera.
- **Corrección por número de pruebas** (`robustez.corregida`): `tipo` corto para el chip de la web (`holm`, `bh` = q de
  Benjamini-Hochberg, `dsr` = Sharpe deflactado, `bonferroni`, `p`, o `sin_p`), `metodo` (la frase larga), `valor` y
  `pasa`. Si un archivo no trae `tipo`, se deduce del método. **Sin corrección siendo una de varias configuraciones de su
  estudio** (`probadas > 1`; las 378 salidas, las rejillas…): `{"tipo": "sin_p", "metodo": "sin p ni corrección: una de
  N configuraciones de su estudio", "valor": null, "pasa": false}`. E2 con 7 días lleva `sin_p` porque su salida se eligió
  después de ver los datos. `corregida: null` solo para una configuración suelta (los presets del motor).
- **Frágil** (`fragil_motivos`): gana fuera y pierde dentro; sin sus 5 mejores operaciones pierde; no pasa la corrección
  (`pasa: false`); o no la tiene (`sin_p`: «sin corrección por número de pruebas: una de N configuraciones de su
  estudio, que sube en el ranking por su resultado fuera de muestra»). El estudio puede añadir los suyos (`fragil: true`
  y `fragil_motivos`: E2 con 7 días, «la salida de 7 días se eligió después de ver los datos: no está validada»).
  `poca_muestra`: menos de 30 operaciones fuera. Con los estudios de hoy, todas las que entran son frágiles.
- **Limpieza.** Todo se limpia al leerlo (números finitos, textos cortos, `en_vivo` solo con números, booleanos y
  textos): una fila o una tarjeta rota se salta y se apunta en `fuentes.estandar.errores` («x.json: 1 fila no vale: …»)
  o `fuentes.lab_vivo.errores`, sin tumbar el ranking ni dejar NaN en el JSON.
- **Orden y entrada.** `pct_dia` de mayor a menor; desempate `op_dia` y `fuera.n`. Entran las que no pierden fuera de
  muestra (`fuera.R ≥ 0` con al menos una operación); las demás llevan `posicion: null`. `en_marcha` (la del motor,
  por `openings.params.preset` o, sin él, `DATA_DIR\estrategia_activa.json`) se enseña siempre aparte. Presets → filas:
  `ruptura3`, `seis2`, `seis`, `impulso` → `e2_impulso_7d`, `seis4r` → `salidas_motor:F0 stop 2 % · objetivo 4R · tope 7d` y `seis4r3` → `salidas_motor:F0 stop 3 % · objetivo 4R · tope 7d`
  (`ranking.SEIS4R_FILA`: «6 diarias 4R» es esa salida de `salidas_motor.py` sobre las entradas de «6 diarias», con las
  mismas operaciones; su fila es la del estudio, sin otra «seis4r» repetida, y suma las operaciones del paper con
  `tags.preset` `seis4r`).
- **Estado.** `descartada_por_el_usuario` (E2: sus dos filas) manda; luego `en_paper` (la que opera el motor), luego
  `en_avance` (vigilancia o laboratorio vivo en avance); si no, `backtest`.
- **Nueva y puesto de antes.** `nueva`: entró en las últimas 24 h; `posicion_antes`: su puesto en el ranking anterior
  distinto. Se guardan en `DATA_DIR\ranking_estado.json` (sobreviven a un reinicio; la primera vez, todo lo que hay se da
  por visto).
- **Mensaje del bus** (estado, va en la foto inicial; al arrancar y cada 60 s, solo si cambia):
  `{"type": "ranking", "ts", "n_total", "n_ranking", "filas": [las 20 primeras, sin perfiles ni archivo_ranking],
  "en_marcha", "nota", "fuentes": {estandar|lab_vivo|paper|vigilancia: {ts, filas, estado}}}`.
- `en_marcha`: `{id, preset, nombre, desde, pausada, fuente ("motor"|"estrategia_activa.json"), posicion, n_ranking,
  en_ranking, texto, fila}`; `texto`: «Puesto 24 de 863 en el ranking», «Fuera del ranking: pierde fuera de muestra
  (−0,075 R por operación)» (solo con R < 0), «Fuera del ranking: no pierde (+0,120 R por operación), pero le faltan las
  operaciones por jornada para calcular su % de la cuenta al día» (o «…pero no tiene operaciones fuera de muestra»),
  «Fuera del ranking: sin medida fuera de muestra» o por qué no está.
- **Web:** pestaña «Ranking» (`#ranking`) y su resumen en «Mando» (las 5 primeras, con «frágil» o «descartada por ti»,
  y la que está en marcha; sin respuesta del ranking, la nombra por `openings` con «sin datos del ranking»). La
  demostración lleva el ejemplo incrustado en `<script type="application/json" id="rankingData">`: las filas reales
  (Ruptura V3, E2 con 7 días, «6 diarias», «6 diarias v2» y E1 y E2 del laboratorio) son filas normalizadas como las
  enseña el bot y las rehace `scripts/ranking_importar.py --demo`; las que empiezan por «Ejemplo» son inventadas. En
  directo el servidor quita el bloque de la página (`server.BT_BLOCK`).

## 3. API de solo lectura (servidor en 127.0.0.1:8765)

| Ruta | Devuelve |
|---|---|
| `GET /api/estado` | Resumen para la IA local (precios, flujo, libro, agenda, fuentes, IA local y cartera). Desde el 28-10-2026, en lugar de `bolsas` (la variación de cada índice) lleva `aperturas_cripto`: por bolsa, `{nombre, estado, apertura (Madrid), minutos_desde_apertura, medida ("desde la apertura"\|"a las 4 h"\|"última apertura, a las 4 h"), cripto {moneda: {pct, rango_pct}} \| null, media_pct}` del último `cripto_aperturas` |
| `GET /api/noticias?horas=&min=&bolsa=` | Noticias con su triaje. Cada una lleva además `medio`, `estado_fuente`, `peso_fuente`, `peso_medio`, `clase` (`cripto\|macro\|contexto`, del prefiltro) y `reflejo` (el último del evento `reflejo`, o `null`); `credibilidad_ajustada` = la del nivel y las confirmaciones × peso del medio × peso de la fuente (0 si está silenciada, y 0 si es macro o geopolítica sin reflejo confirmado en la cripto); `confirmaciones` no cuenta las de fuentes silenciadas |
| `GET /api/fuentes?region=&estado=` | Lista de feeds (2.1) con `estado` y `peso` de su ciclo de vida (2.12); `estado=` filtra |
| `GET /api/tendencia` | Último `trend` y `orderflow` |
| `GET /api/predicciones` | Último `predictions` |
| `GET /api/backtest` | Contenido de `BACKTEST_DIR/resultados.json` (3.1) |
| `GET /api/backtest/aperturas` | `BACKTEST_DIR/aperturas.json`: el motor anterior (v2 «ruptura»), solo como referencia (404 sin archivo) |
| `GET /api/backtest/impulso` | `{impulso, lab}` (`bot/resumen_backtest.py`): `impulso` = `BACKTEST_DIR/impulso_7d.json` (lo escribe `controles/compara_impulso.py`), la variante de 7 días que opera el motor, con `fuera` y `dentro` = `{n, mean_R, win_rate, salidas, horas_media, largos, cortos, desde, hasta, total_R, max_dd_R, curva_R [[ms, R acumulado]] (≤ 151 puntos)}` y, si la curva del archivo trae un punto por operación, `max_R` y `min_R` (la mejor y la peor), `ganadoras_n`, `ganadoras_bajo_2R` y `ganadoras_bajo_1R` (cuántas ganadoras ganan menos de 2R y de 1R: el cierre por tiempo sale con lo que haya), calculados con la curva completa (`resumen_backtest.per_trade`) y `nota`; `lab` = resumen de `laboratorio.json` (`summary`, `promising`, `robust`, `avance` = cuenta honesta en avance trimestral con 0,5 % de riesgo); `seis` = `BACKTEST_DIR/seis_diarias_resumen.json` (la réplica del motor con el preset «6 diarias»), con la misma forma que `impulso` más `nombre` y `params`; `seis2` = `BACKTEST_DIR/seis2_resumen.json` (la réplica del motor con el preset «6 diarias v2»: la mitad en 3R y el resto con el cerrojo; `salidas` con `stop`, `tiempo`, `cerrojo`, `seguimiento` y, si las hay, `tiempo (activada)` y `fin de datos`), con la misma forma que `seis`; `ruptura3` = `BACKTEST_DIR/ruptura3_resumen.json` (29-10-2026, lo escribe `scripts/replica_ruptura3.py`: la réplica del motor con el preset «Ruptura V3», operación a operación igual que `controles/selectiva/medir_selectiva.py`), con la forma de `seis2` y además, en `fuera` y `dentro`, `op_jornada`, `stop_medio_pct`, `paper_R_funding` y `antes_de_costes {n, R_bruto, deslizamiento_R, comisiones_R, funding_R, R_neto, costes_R, gana_bruto_pct, gana_neto_pct, stop_medio_pct, cuenta_jornada_bruto_pct, cuenta_jornada_costes_pct, cuenta_jornada_neto_pct, op_jornada}` (las mismas operaciones sin comisiones ni deslizamiento ni funding y cuánto se lleva cada coste, en R por operación y en % de la cuenta por jornada con 0,5 % de riesgo). Cada resumen y la respuesta entera llevan `costes` (29-10-2026, `resumen_backtest.costes`): `{perfil ("okx_eu_con_xperps" si la comisión es la de tomador del contado con X-Perps; si no, null), comision_pct, comision_perp_pct?, deslizamiento_bps, coste_rt_pct, texto, aviso, perfil_en_uso}`: las réplicas y el laboratorio van con 0,10 % por lado (también en los perpetuos de las réplicas; el laboratorio, 0,05 % en perpetuos) y 2 pb; el aviso dice que sin cuenta X-Perps (0,35 % y sin cortos) serían peores. `seis4r` = `BACKTEST_DIR/seis4r_resumen.json` (29-10-2026, lo escribe `scripts/replica_seis4r.py`: la réplica del motor con el preset «6 diarias 4R», operación a operación igual que la variante «F0 stop 2 % · objetivo 4R · tope 7d» de `controles/salidas_motor.py`), con la forma de `ruptura3` (`salidas` con `stop`, `objetivo` y `tiempo`; `params.preset` `seis4r`) y además, en `fuera` y `dentro`, `perfiles {sin_costes, backtest, okx_eu_con_xperps, okx_eu_sin_xperps}` = `{n, R, gana_pct, op_dia, pct_dia, pct_dia_natural, R_total}` (las claves de `ranking.PERFILES_TEXTO`: las mismas operaciones sin costes, con los del backtest y con los de OKX EU con X-Perps; sin X-Perps, el libro rehecho solo con las compras), y `dentro.largos`/`dentro.cortos`. Lo que falte va como `null`; 404 si faltan los seis. La demostración (sin bot) los lleva incrustados en `<script type="application/json" id="seisData">`, `id="seis2Data"`, `id="ruptura3Data"` e `id="seis4rData"` (`scripts/incrustar_backtest.py`); en vivo, la web los quita y los pide aquí |
| `GET /api/noticias_fiables` | Último `noticias_fiables` (2.15); 404 si aún no hay ningún cálculo |
| `GET /api/decisiones?desde=<ms>&ventana_id=&coin=&limite=` | `{rows, n, desde}`: filas de `decisiones_apertura` (2.17) con `ts ≥ desde` (por defecto, los 3 últimos días), de esa apertura y moneda si se piden, la más reciente primero (`ts` y luego `id`), como mucho 2.000 (`limite` de 1 a 2.000). `datos` como objeto. 400 con un parámetro no válido; 404 si el módulo no está en marcha (entonces no se guardan). No va en el estado inicial del WebSocket |
| `GET /api/velas?coin=BTC&desde=<ms>&tf=1m\|5m\|15m&hasta=<ms>&fuente=okx\|bot` | Velas de una moneda para la gráfica de cada posición abierta (29-10-2026): `{coin, tf, desde, hasta, hasta_datos, recortada, n, velas: [[t, o, h, l, c]], fuente, mercado, instId, nota}`. **`fuente=okx` (por defecto):** las velas REALES de OKX EU del par que se operaría (`instId` `BTC-USDC`, `ETH-USDC`, `SOL-USDC`, `XRP-USDC`, al contado), de las rutas públicas `GET https://eea.okx.com/api/v5/market/candles` y `…/history-candles` (sin claves ni firma; `bot/velas_okx.py`: caché en memoria, la vela en curso se repide como mucho cada 5 s, ≤ 10 peticiones cada 2 s), del tamaño pedido (`bar` 1m, 5m o 15m de OKX, sin agrupar), con `mercado: "OKX EU"` y `nota: "velas de OKX EU, BTC-USDC"`; la última puede estar en curso. Si OKX no responde o no tiene velas en el tramo, caen a las del bot y lo dicen: `fuente: "bot"`, `pedida: "okx"` y `aviso` («OKX EU no respondió (…): velas del bot»). **`fuente=bot`:** solo las que el bot ya tiene (`mercado: "Binance (al contado)"`, `instId` `BTCUSDT`, `nota: "velas del bot (Binance, BTCUSDT, 1 min)"`) (`Services.velas` = `server.velas_de`: las de la vigilancia en memoria si llegan hasta `desde`, que es lo normal porque guarda semanas; si no, la caché de 1 min de `HIST_DIR/1m` más la memoria; cerradas y con 1-2 min de retraso); nunca pide nada a la red. Las de 1 min se agrupan a `tf` (apertura de la primera, máximo, mínimo y cierre de la última; la última vela puede ir a medias) y se saltan las filas con precios no válidos. `t` = apertura de la vela (ms); `desde` = la primera devuelta; `hasta` por defecto ahora y `desde` por defecto 3 h antes; `hasta_datos` = hasta dónde llegan las velas (lo de después lo pone la web con el precio en vivo): con `fuente=bot`, el fin de la última vela de 1 min que había; con `fuente=okx`, el fin de la última si está cerrada y, si está en curso, la hora de la respuesta (no el fin de esa vela, que aún no ha llegado). Un 404 lleva texto: «Sin velas de … en la caché del bot en ese tramo (…)» o «Las velas no están disponibles: …»; la web lo distingue del 404 de un bot sin la ruta. Como mucho 2.000 velas, las más recientes (`recortada: true` si no caben desde `desde`). 400 con un parámetro no válido (`coin` fuera de `config.COINS`, `tf` desconocido, números no enteros o `desde ≥ hasta`); 404 sin la fuente de velas o sin ninguna vela en ese tramo. No va en el estado inicial del WebSocket |
| `GET /api/resultados` | Operaciones paper y agregados (3.2), con las comisiones pagadas, el funding, el bruto y el neto |
| `GET /api/real` | El panel «Cuenta real» (2.18): `RealBroker.vista_real()` (lo mismo que el mensaje `cuenta_real`), o `{"modo": "apagado", "broker": "paper", "resumen": "Modo real apagado: el bot opera en paper", …}` sin el conector. Nunca lleva un valor de las claves |
| `GET /api/vigilancia` | Último `vigilancia` (2.8) y `operaciones`: las operaciones virtuales que cuentan, de la más reciente a la más antigua (filas de la tabla `vigilancia`, hasta 500) |
| `GET /api/ranking?origen=a,b&cumple=1\|0&min_op_dia=&limite=300&fragil=1\|0&todas=1` | El ranking de estrategias (2.20): `{ts, n_total, n_ranking, n_filtradas, filas, en_marcha, fuentes {estandar, lab_vivo, paper, vigilancia}, nota, criterio, origenes [{id, nombre, n, n_ranking}], perfiles_costes [{id, texto, n_ranking}], filtros}`. `filas`: las que entran, en su orden (con `todas=1`, también las que no entran, detrás, con `posicion: null`); los puestos son los del ranking entero (un filtro no los cambia). `origen`: uno o varios separados por comas; `cumple`: solo las que cumplen (1) o no (0) tus reglas; `min_op_dia`: operaciones por jornada como mínimo (admite coma decimal); `fragil=0` oculta las frágiles; `limite`: entero, por defecto 300; fuera de 1-5.000 se recorta (como los demás `limite` de la API). 400 con un parámetro no válido (`origen` con otros caracteres, `min_op_dia` que no es un número ≥ 0, `cumple`/`fragil`/`todas` que no son 1 o 0, `limite` que no es un entero). Lleva `ETag` (la firma del contenido sin las horas de cálculo, `Ranking.firma`) y `Cache-Control: no-cache`: con `If-None-Match` igual contesta **304** sin cuerpo (la tabla entera pesa ~2 MB y la web la vuelve a pedir cada 2 min y con cada mensaje `ranking`). Sin el módulo, el servidor crea el suyo con lo que hay. No va en el estado inicial del WebSocket (el mensaje `ranking` sí) |
| `GET /api/lab_vivo` | `estado.json` del laboratorio vivo más `mejores` (de `mejores.json`, hasta 20; sin él, las de `estado.json`), `diario` (el de hoy o `null`) y `pausa_manual`. Sin archivo: `{"estado": "apagado", "nota": "El laboratorio vivo no está en marcha (scripts/lab-vivo.ps1)"}`; con el latido de hace más de 3 min: `estado: "parado"`, `estado_corredor` (el que decía) y `nota`, con el resto como estaba |
| `GET /api/lab_vivo/hipotesis?estado=&familia=&limite=100` | `{hipotesis, n, ts}` de `hipotesis.json`, filtradas (`limite` de 1 a 1.000) |
| `GET /api/lab_vivo/hipotesis?id=h-AAAAMMDD-HHMM-…` | `hipotesis\<id>.json` (la hipótesis, su admisión y cada variante con su tarjeta, sus pruebas y sus operaciones); 400 si el `id` no cumple `^h-\d{8}-\d{4}-[a-z0-9_-]{3,60}$` (nada de rutas); 404 si no existe |
| `GET /api/lab_vivo/diario?dia=AAAA-MM-DD` | El diario de ese día (por defecto, hoy en Madrid); 400 con otro formato; 404 si no hay |
| `GET /api/lab_vivo/cola` | `cola.json` (sin archivo, `{"estado": "apagado", "pendientes": 0, …}`) |
| `GET /api/lab_vivo/generador?limite=10` | `{notas, n}`: las últimas notas del agente generador (`generador\*.json`, de la más reciente a la más vieja, con `archivo`; `limite` de 1 a 50) |
| `GET /api/aperturas` | Último `openings` (2.9) |
| `GET /api/cripto_aperturas` | Último `cripto_aperturas` (2.14), o `{"estado": "apagado"}` sin el módulo |
| `GET /api/aprendizaje` | Último `aprendizaje` (2.10) más `medios` (ranking de 60), `postmortems` (20), `cambios` (30 cambios de peso) y `noticias` `{mejores, peores}` (las que mejor y peor funcionaron a 4 h, 20 de cada) |
| `GET /api/aprendizaje/medios?orden=peso\|acierto\|casos\|ruido\|ops\|R&limit=&veredicto=&min_casos=&q=` | Ranking de medios del último cálculo |
| `GET /api/aprendizaje/medio?id=dominio` | Todo lo medido de un medio (404 sin datos) |
| `GET /api/aprendizaje/noticias?cuales=mejores\|peores&horizonte=15\|60\|240\|1440&dias=30&limit=20&solo_senal=1` | Noticias con titular, medio, rendimientos, `explicacion` y `operaciones` [{operacion, postmortem}] |
| `GET /api/aprendizaje/estadisticas?por=nivel\|region\|idioma\|tipo\|ventana\|fase\|rol\|moneda` | Acierto por esa dimensión |
| `GET /api/aprendizaje/postmortems?limit=&origen=paper\|vigilancia` | Post-mortems: `resumen` (sin IA), `texto` (IA local o null), `estado` (`pendiente\|hecho\|error`) y `hechos` |
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

Las órdenes de la web al bot (aprobar, cerrar, cambiar el modo, detener, reanudar, cancelar y el perfil de comisiones; los mandos del modo real; sobre fuentes, pesos de medios y evolución; y pausar o reanudar el laboratorio vivo) van por el WebSocket `/events` (ver 3.3).

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
{"start_equity": 10000, "equity": 10012.3,
 "trades": [{"id": "t…", "story": "n…", "strategy": "…", "window": "Apertura Londres", "market": "londres", "coin": "SOL",
             "side": "long", "qty": 3.1, "entry": 104.9, "exit": 106.0, "sl_initial": 103.2, "trail_pct": 1.2,
             "opened": 0, "closed": 0, "reason": "Stop dinámico", "pnl": 3.4, "pnl_pct": 1.05, "R": 0.7, "fees": 0.65,
             "headline": "…", "news_score": {"importancia": 7, "credibilidad": 80},
             "instrument": "spot|perp", "funding": 0.0, "commissions": 0.0, "tags": {},
             "costes": {"perfil": "okx_eu_con_xperps", "entrada": 3.25, "salidas": 2.73, "comisiones": 5.98,
                        "funding": 0.0, "total": 5.98, "entrada_R": 0.065, "salidas_R": 0.055, "funding_R": 0.0,
                        "total_R": 0.12, "salida_tipo": "creador|tomador", "estimado?": true},
             "bruto": 9.38}],
 "by_strategy": [{"strategy": "…", "n": 4, "win_rate": 0.5, "pnl": 12.0, "avg_R": 0.3}],
 "by_window": [{"window": "…", "n": 0, "win_rate": 0, "pnl": 0}], "by_coin": [{"coin": "BTC", "n": 0, "win_rate": 0, "pnl": 0}],
 "equity_curve": [[0, 10000.0]], "daily": [{"date": "2026-10-23", "pnl": 0, "pnl_pct": 0, "n": 0}],
 "comisiones_total": 5.98, "funding_total": 0.0, "costes_total": 5.98, "bruto": 9.38, "neto": 3.4,
 "costes_estimados": 0, "comisiones": { /* portfolio.comisiones (2.5) */ }}
```

- **Comisiones (29-10-2026):** cada operación lleva `costes` (comisión de `entrada`, de `salidas` —parciales y la última—, `funding` + cobrado / − pagado y `total` = comisiones − funding, en USDC y en R; `perfil` con que se abrió; `salida_tipo` de la última salida: `creador` en el objetivo, `tomador` a mercado) y `bruto` (`pnl` + `total`: el resultado antes de comisiones y funding, con el deslizamiento en los precios). Las cerradas antes de esa fecha no guardaban el detalle: se calcula al leerlas (`estimado: true`; pagaron 0,10 % a la entrada, el resto fue la salida) sin reescribirlas. Arriba: `comisiones_total` (lo pagado en comisiones), `funding_total`, `costes_total`, `bruto`, `neto` (Σ `pnl`, el de verdad; `bruto` − `costes_total` = `neto`), `costes_estimados` (cuántas van estimadas) y `comisiones` (el perfil en uso).

- `instrument`, `funding` (USDC; + cobrado, − pagado) y `commissions` (las comisiones de entrada y salida). `fees` = `commissions` − `funding`: el funding entra en el resultado, en `pnl` y en R. Las operaciones del motor de aperturas llevan `tags` (2.6, con `origen: "motor_aperturas"`) y su estrategia es `impulso-<largo|corto>-<noticia-a-favor|noticia-en-contra|sin-noticia>` (también con «6 diarias»: la versión la dice `tags.estrategia_firma` y el preset `tags.preset`). Las de la entrada por noticia (2.16) llevan `tags.origen: "noticia_fiable"` y `tags.story` (la de la noticia), ventana «Noticia fiable» y estrategia `noticia-fiable-<largo|corto>`. El evento `closed` mantiene sus campos de siempre.

### 3.3 Órdenes de la web al bot (WebSocket `/events`)

Por el mismo WebSocket por el que recibe los eventos, la web envía mensajes de texto con un objeto JSON. Las de la primera tabla actúan solo sobre el paper (dinero ficticio); las del modo real, más abajo, van al conector de OKX EU (apagado mientras el usuario no lo active) y exigen el origen estricto. El bot no contesta a cada mensaje: la confirmación es la cartera (`portfolio`, 2.5), que se publica al momento tras cada orden, junto con los eventos de 2.6.

| Mensaje | Qué llama en `PaperBroker` | Qué se ve después |
|---|---|---|
| `{"type": "decision", "story": "n…", "ok": true}` | `decide(story, ok, "Aprobada por ti" o "Rechazada por ti")` | `humanok` (y la ejecución) o `humanko` |
| `{"type": "close", "story": "n…"}` | `close(story, "Cierre manual desde la web")` | `closed`. Sin precio actualizado no se cierra, y el agente `exec` dice por qué |
| `{"type": "mode", "mode": "auto\|veto\|manual"}` | `set_mode(mode)` | `portfolio.mode`. Al pasar a `manual`, lo que esperaba el plazo de veto pasa a esperar tu aprobación (nuevo `humanwait` con 10 min de plazo) y ya no se aprueba solo |
| `{"type": "halt"}` | `halt("Detenida desde la web")` | `portfolio.halted = true` |
| `{"type": "resume"}` | `resume("Reanudada desde la web")` | `portfolio.halted = false` |
| `{"type": "cancel", "story": "n…"}` | `cancel(story, "Cancelada por ti desde la web")` | `humanko` si esperaba tu aprobación, `blocked` si estaba programada |
| `{"type": "comisiones", "perfil": "okx_eu_con_xperps\|okx_eu_sin_xperps"}` | `set_comisiones(perfil)` (29-10-2026): el perfil de comisiones de las entradas nuevas (las abiertas siguen con el suyo), guardado en `paper_state` (clave `comisiones`) salvo que cambie `BOT_COMISIONES`. Con la comisión real de la cuenta leída, manda esa (la elección queda para cuando no la haya) | `portfolio.comisiones` al momento y `orden_web` (`area: "comisiones"`, `accion: "perfil"`, `id` = el perfil); un perfil desconocido, `ok: false` sin tocar el paper |

**Modo real (29-10-2026, docs/06).** Los mandos del panel «Cuenta real» van a `RealBroker` (si no está construido, `orden_web` con `ok: false`) y contestan en `orden_web` (`area: "real"`, `accion`, `ok`, `why`); después sale `cuenta_real` (2.18). **Origen estricto:** todos exigen que el WebSocket se abriera con la cabecera `Origin` de la propia web, exactamente `http://127.0.0.1:<PORT>` o `http://localhost:<PORT>`, y desde este PC (`request.remote` 127.0.0.1 o ::1: con `BOT_HOST=0.0.0.0`, un equipo de la red con el `Origin` falsificado no pasa) (`server.origen_web`; ni `[::1]`, ni `BOT_WS_ORIGINS`, ni sin cabecera); si no, `ok: false` «Solo desde la propia web del bot (cabecera Origin)». La única excepción es la parada (solo reduce el riesgo). **Lo que frena y lo que no:** el `Origin` lo pone el navegador, así que frena a otras webs abiertas en él y a un cliente que no lo mande (los agentes del bot no lo mandan), pero un proceso de este PC puede ponerlo a mano. Contra eso no hay cabecera que valga (un proceso con tu usuario también podría escribir en `secretos\`): lo que ningún proceso se salta son las puertas que pones tú en tu PC, `okx.env` con tus claves, `BOT_MODO_REAL=1` al arrancar, tu frase escrita y `BOT_BROKER=real` (docs/06). Ningún mando escribe ni lee `okx.env`.

| Mensaje | Llama a | Respuesta |
|---|---|---|
| `{"type": "real", "accion": "confirmar", "frase": "ACTIVO EL MODO REAL"}` | `confirmar(frase)` | `{ok, why}` |
| `{"type": "real", "accion": "parar", "cerrar": false}` (también sin `Origin`) | `parada_emergencia("Parada de emergencia desde la web", cerrar)`; solo el `true` de JSON cierra posiciones (la cadena `"true"`, no) | `{ok: true}` (`why` «La parada ya estaba pedida» si no había nada nuevo) |
| `{"type": "real", "accion": "limites", "limites": {"max_usdc": 5}}` | `set_limites(limites)` (bajarlos, o devolverlos como mucho hasta el techo; nunca por encima) | `{ok, why}` |
| `{"type": "real", "accion": "detener"}` / `"reanudar"` | `halt("Detenido desde la web")` / `resume("Reanudado desde la web")` | `{ok}` |
| `{"type": "real", "accion": "aprobacion", "mode": "veto\|manual\|auto"}` | `set_mode(mode)` («auto» solo en la demo de OKX) | `{ok}` |
| `decision`, `close`, `cancel` con una story del modo real (en `real.pending`, `real.en_curso` o `real.positions`) | `real.decide` / `real.close` / `real.cancel` (con el mismo `ok` booleano estricto) | `orden_web` (`area: "real"`) y los eventos del modo real |

Órdenes sobre lo que mide y aprende (no tocan el paper ni las reglas del usuario; se atienden aunque no haya paper):

| Mensaje | Qué llama | Qué se ve después |
|---|---|---|
| `{"type": "fuente", "accion": "restaurar\|silenciar\|retirar\|activar\|a_prueba\|anadir", "id": "<feed> o medio:<dominio>", "motivo": "…"}` | `FuentesVivas.accion` (`anadir`: `id` = dominio de un candidato verificado) | `fuentes_vivas` (2.12) y `orden_web` (2.13); el cambio queda en `evolution_log` con tu motivo |
| `{"type": "peso_medio", "medio": "dominio", "peso": 1.2, "motivo": "…"}` (`peso: null` vuelve al automático: botón «Volver al automático» del ranking de medios) | `Aprendizaje.fijar_peso` | `orden_web`; el cambio queda en `medios_peso_hist` y la evolución lo copia a `evolution_log` |
| `{"type": "revertir_peso", "id": 12}` | `Aprendizaje.revertir`: deja el medio como estaba justo antes (en automático si lo estaba y eso lo devuelve allí; si no, fijado a mano en el peso de antes); una fila `veredicto` no se revierte | `orden_web` |
| `{"type": "evolucion", "accion": "aplicar\|descartar\|revertir\|evaluar\|modo", "id": "p…\|v…", "motivo": "…", "modo": "proponer\|automatico"}` | `Evolucion.orden_web` (aplicar, revertir, evaluar, modo) o `Evolucion.atender` (descartar) | `evolucion.ultima_orden`; aplicar, revertir y el modo quedan en `evolution_log` por la evolución, y descartar como `propuesta_descartada` (origen `web`) |
| `{"type": "lab_vivo", "accion": "pausar\|reanudar"}` (29-10-2026; **origen estricto**, como el modo real: `server.origen_web`) | `LabVivoLectura.mando`: `pausar` crea `DATA_DIR\lab_vivo\pausa_manual` (el corredor no empieza nada mientras exista) y `reanudar` lo borra. No hay más mandos: ni criterios, ni cupos, ni aprobar nada desde aquí | `orden_web` (`area: "lab_vivo"`, `accion`, `ok`, `why`: «Ya estaba en pausa», «No estaba en pausa», «Solo desde la propia web del bot (cabecera Origin)»…) y el mensaje `lab_vivo` (2.19) al momento |

- Las de pesos también se admiten como `{"cmd": "aprendizaje_peso", "outlet": …}` y `{"cmd": "aprendizaje_revertir", "id": …}`.
- `id` de una fuente: cadena de 1 a 300 caracteres; `medio`: de 1 a 200; `peso`: número de JSON (no booleano ni texto) o `null`; `id` de un cambio de peso: entero. Lo que no cumple se registra y se ignora.

Reglas de los mensajes:
- `ok` tiene que ser un booleano de JSON (`true` o `false`). La cadena `"false"`, un número o la falta del campo no aprueban ni rechazan nada: la orden se ignora y queda registrada.
- `story` tiene que ser una cadena de 1 a 128 caracteres. Si no lo es, la orden se ignora.
- Un `type` desconocido se registra y se ignora. Un JSON roto se ignora.
- La web solo envía `halt`, `resume`, `cancel` y `comisiones` si `portfolio.caps` los incluye. Un bot que no los admite los registra y los ignora.

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
- `trades(id TEXT PRIMARY KEY, story TEXT, opened INTEGER, closed INTEGER, body TEXT)` (paper trading). Desde el 29-10-2026 la abierta guarda `comision {perfil, creador, tomador}` y la cerrada `costes` y `bruto` (3.2). `paper_state(key, body)`: además de `queue`, `mode`, `halt`, `manual_halt`, `day_start`, `start_equity` y `cuenta`, `comisiones` = `{perfil, config}` (el perfil elegido desde la web y el `BOT_COMISIONES` de entonces). `day_start` = `{date, equity}`: el capital con que empieza el día de Madrid (base de la pérdida del día), a precio de mercado en la primera ronda tras la medianoche; desde el 29-10-2026, si el bot arranca después de la medianoche sin esa foto y con posiciones abiertas desde antes, se rehace con lo cerrado antes de la medianoche y esas posiciones a su último precio guardado (`last`), con `origen: "ultimo_precio_guardado"`. Sin posiciones de antes no hay foto: cuenta lo realizado.
- `real_trades(id TEXT PRIMARY KEY, story TEXT, opened INTEGER, closed INTEGER, body TEXT)` y `real_state(key TEXT PRIMARY KEY, body TEXT)` (modo real, docs/06 apartado 6; las crea `RealBroker` al construirse, aditivas: el paper no se toca). `real_trades`: una fila por operación real; abiertas con `closed` NULL (incluidas las «enviando»), cerradas con el formato 3.2 más `instId`, `modo`, `entorno` (`demo`/`real`), `cuenta` (12 caracteres de un SHA-256), `cl` y `partes`; las abiertas, además `exp_ms`, `cierre_pedido`, `cierres_dudosos` y, por parte, `cls`/`dudosas`. `real_state`: `limites`, `aprobacion`, `manual_halt`, `halt`, `parada` (la parada de emergencia pegajosa, con `archivo` y `hecha`) y por cuenta `day_start:<cuenta>` e `inicio:<cuenta>`. Nunca guardan claves (pasan por `okx_firma.limpiar`).
- `decisiones_apertura(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, ventana_id TEXT NOT NULL, ventana TEXT, dia TEXT, apertura_ms INTEGER, coin TEXT NOT NULL, fase TEXT, decision TEXT, motivo_codigo TEXT, motivo TEXT, impulso_pct REAL, lado TEXT, preset TEXT, estrategia TEXT, trade_id TEXT, story TEXT, datos TEXT)` (por qué entra o no, 2.17). Índices `(ventana_id, coin)` y `(ts)`. Solo se añade: dos disparadores impiden DELETE y UPDATE, y `store.prune` no la toca. `fase`: `medida|entrada|riesgo|paper|cierre|perdida`; `decision`: `entra|no_entra|rechazada|perdida|abierta|cerrada|esperando`; `motivo_codigo`: el catálogo cerrado de 2.17; `lado`: `largo|corto|null`; `datos`: JSON (2.17). Migración aditiva (`store.migrate_decisiones`, al abrir el `Store`): crea la tabla si falta y, en una base con una versión anterior, añade las columnas que le falten sin tocar sus filas. Las ids las reparte el bot en memoria (siguen a la mayor guardada) y las guarda un hilo.
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
  - Archivo `DATA_DIR/estrategia_activa.json` (escritura atómica): `version, estrategia "impulso", fuente preset|evolucion|usuario, params, lab_id, lab_twin, twin_filtro, codigo, nombre, pausada, desde, actualizado, motivo, registro, propuesta, firma, solo_paper: true` y, si lo escribió `scripts/activar_estrategia.py` (fuente `usuario`, o `preset` al volver a E2), `preset` (`seis4r|ruptura3|seis2|seis|impulso`) y `descripcion`. Lo lee el motor (2.9) y nunca vale si rompe las reglas del usuario. Un ajuste de la evolución sobre un preset quita `preset` y `descripcion` (ya no es el preset).
- Noticias fiables (2.15; `bot/fiables.py`, que `store.prune` no toca):
  - `noticias_fiables(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, json TEXT)`: un registro por cálculo nuevo, con el mensaje `noticias_fiables` entero en `json`. Solo se añade: dos disparadores impiden DELETE y UPDATE. Con los mismos datos de entrada (`firma_datos`) no se repite; al arrancar se carga el último.
- Fuentes vivas (2.12; `bot/fuentes_vivas.py`):
  - `source_status(id, estado, desde, motivo, tipo, manual, origen, previo, nombre, spec, cifras)`: manda sobre el registro `bot/fuentes/*.json` (un feed sin fila está activo); `id` = id del feed o `medio:<dominio>`.
  - `source_status_log(ts, id, antes, despues, tipo, motivo, cifras, auto, por)`, `source_health(id, horas, desde)`, `fv_noticias` (cada noticia captada con feed, medio, hora, latencia, veredicto, si se trió y si la confirmaron; 120 días) y `fv_candidatos` (medios candidatos con su RSS y su estado).
- Ranking de estrategias (2.20; archivos, no tablas): `BACKTEST_DIR\ranking\*.json` (una fila común por archivo; los escribe `scripts/ranking_importar.py`, que solo escribe ahí, y cualquier estudio nuevo; el bot solo los lee) y `DATA_DIR\ranking_estado.json` (`{formato, ts, vistas {id: ms en que entró (0 = ya estaba)}, actual {id: puesto}, anterior {id: puesto}, cambio}`: lo único que escribe `bot/ranking.py`, de forma atómica).
- Laboratorio vivo (2.19): su base `DATA_DIR\lab_vivo\lab_vivo.db` **no** está en `bot.db` y el bot no la abre (esquema en docs/07, apartado 12). El servidor solo crea o borra `DATA_DIR\lab_vivo\pausa_manual` (3.3).
- Histórico de noticias de 3 años (29-10-2026; archivos, no tablas; `bot/noticias_historia.py`, `bot/noticias_informe.py` y `bot/lab_vivo/noticias.py`; docs/07, apartado 21). Todo en `DATA_DIR\noticias_historia\` (o `BOT_NOTICIAS_HISTORIA_DIR`); el bot en marcha no lo lee ni lo escribe, y ninguna ruta ni mensaje del bus cambia:
  - `gdelt\lista\gkg-<desde>-<hasta>.json` (`[[AAAAMMDDHHMMSS, bytes, md5, url]]` de la lista maestra de GDELT 2.0), `gdelt\dias\AAAA-MM-DD.jsonl.gz` (una fila por artículo cripto o macro del GKG en inglés: `{id, t, s, dom, url, tit, clase, mon, temas, macro, tono, gdelt}`, con `t` la hora de GDELT y `s` el sello, en ms UTC) y `AAAA-MM-DD.json` (su resumen: `archivos`, `archivos_ok`, `faltan`, `completo`, `filas`, `dominios`; existe solo con el día hecho).
  - `wp\<medio>\AAAA-MM.jsonl.gz` (los artículos cripto o macro de los 24 medios de cripto por la API de WordPress: `{id, t, s, dom, url, tit, cat, clase, mon, temas, macro}`, `t = s = date_gmt`) y `AAAA-MM.json` (su resumen).
  - `medios_vivos.json` (`{creado, nota, medios}`: los medios que ve la mesa del histórico, congelados el 29-10-2026), `historias\AAAA-MM.jsonl.gz` (`{id, t0, s0, tit, dom, fuente gdelt|wp, url, clase, mon, temas, macro, medios [[dominio, sello, fuente]], n_piezas, n_medios, n30}`) con `historias\resumen.json`, y `triaje\AAAA-MM.jsonl` (`{id, uso, src, tier, region, ts, triage}`, con `triage` en el formato del triaje v2, 2.7).
  - `lab\noticias.npz` y `lab\meta.json` (lo que lee el laboratorio vivo; columnas en docs/07, 21.4) y `informe.json` e `informe.md` (cobertura y calidad).
  - Mandos: `parar` (descargas y `seguir`) y `parar_triaje`; registros `gdelt.log`, `wp.log`, `historias.log`, `triaje.log` y `seguir.log`; `seguir.lock` y `seguir_hecho.json`.
