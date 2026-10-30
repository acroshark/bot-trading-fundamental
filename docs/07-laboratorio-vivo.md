# Laboratorio vivo: un agente 24/7 que genera y prueba estrategias (diseño e implementación, 29-10-2026)

**Estado: fase 1 programada (29-10-2026); el corredor NO está en marcha 24/7.** La primera tanda (una pasada sobre el
catálogo inicial) se hizo el 29-10-2026 y no dejó ninguna candidata (apartado [20.7](#207-primera-tanda-29-10-2026-1548-1633)).
Una revisión adversarial de esa tarde encontró 2 fallos graves y 9 medios; están arreglados y probados (apartado
[20.8](#208-revisión-adversarial-y-arreglos-29-10-2026)).
Este documento es el contrato de
`bot/lab_vivo/`, sus pruebas (`tests/test_lab_vivo*.py`), `scripts/lab-vivo.ps1` y los datos de
`M:\BotFundamental-datos\lab_vivo\`. El cableado con el servidor y la web lo hace el coordinador después (apartado 14).
Hasta entonces el bot en marcha no cambia en nada: no importa `bot.lab_vivo` y nadie lo arranca. Lo que se programó y en
qué se aparta del diseño está en el apartado [20](#20-implementación-29-10-2026).

**Todo es paper.** El laboratorio no opera ni envía órdenes. Tampoco toca la cartera paper, `estrategia_activa.json` ni
`bot.db`. Lo que supera todas las pruebas pasa a una **prueba en avance virtual**. Si la supera, se **propone** al usuario
en la pestaña Evolución. El paso a la cartera paper lo decide el usuario.

Índice:
[0](#0-en-pocas-líneas) · [1 Petición](#1-qué-pidió-el-usuario-y-cómo-se-traduce) ·
[2 Qué esperar](#2-qué-esperar-honestidad-con-los-datos) · [3 Arquitectura](#3-arquitectura) ·
[4 Datos](#4-datos-periodo-y-causalidad) · [5 Hipótesis](#5-hipótesis-formato) · [6 Familias](#6-familias-api-de-plugins) ·
[7 Libro](#7-el-libro-la-gestión-verificada-del-paper) · [8 Validación](#8-validación-por-etapas) ·
[9 Número de pruebas](#9-corrección-por-número-de-pruebas-acumulado) ·
[10 Candidata y lista](#10-candidata-y-lista-para-proponer-criterios-v1) · [11 Corredor](#11-el-corredor-247) ·
[12 lab_vivo.db](#12-lab_vivodb) · [13 Salidas](#13-salidas-para-la-web-y-para-el-generador) ·
[14 Cableado](#14-contrato-de-cableado-para-el-coordinador) · [15 Generador](#15-el-agente-generador) ·
[16 Vigilante](#16-scriptslab-vivops1) · [17 Pruebas](#17-pruebas) · [18 Fases](#18-plan-por-fases-y-criterios-de-aceptación) ·
[19 Decisiones y riesgos](#19-decisiones-del-usuario-y-riesgos) ·
[20 Implementación](#20-implementación-29-10-2026) · [21 Noticias de 3 años](#21-noticias-de-3-años-29-10-2026)

---

## 0. En pocas líneas

- **Qué es.** Un proceso aparte, 24/7 y con prioridad baja: el **corredor**. Recibe **hipótesis** en JSON y las somete a
  una validación por etapas. La validación usa la gestión verificada del paper, las comisiones reales de
  `bot/comisiones.py`, el funding histórico y la corrección por el número de pruebas acumulado.
- **Quién genera.** Un **agente generador**: una tarea programada de Claude, 4 veces al día. Escribe hipótesis con
  fundamento y, si hace falta, una familia nueva con sus pruebas.
- **Quién decide.** El laboratorio mide y el usuario decide. Nada pasa solo a la cartera.
- **La meta.** El usuario pide ≥ 1 % diario. Con el riesgo fijo de 0,5 % por operación son **2 R al día natural**. Para
  cada candidata se informa cuánto le falta. No es un filtro (2.4).
- **Lo que se espera,** con lo medido hasta hoy: que durante semanas no salga nada que lo pase todo. Eso también es un
  resultado. La web lo enseña con lo más cerca que ha estado algo y con el motivo de cada fallo.

## 1. Qué pidió el usuario y cómo se traduce

| Petición (29-10-2026) | Diseño |
|---|---|
| «Una estrategia sólida, rentable a largo plazo» | Sólida = pasa todo esto (apartados 8 a 10): fuera de muestra con corte fijo y avance trimestral, base emparejada, sin sus 5 mayores operaciones y sin su mejor semana, estrés de costes, corrección por número de pruebas y prueba en avance. |
| «Que devuelva rentabilidad desde el minuto uno» | No se puede prometer. Desde el primer día se ven la prueba en avance virtual de cada candidata y la cuenta honesta del laboratorio (9.6). |
| «Con bastantes operaciones» | Clases de frecuencia fijadas ahora (8.3): **frecuente**, ≥ 1 operación por día natural; **selectiva**, de 0,25 a 1. Por debajo de 0,25 no pasa. |
| «Un agente que esté activo 24/7» | Corredor en un proceso aparte con su vigilante (`scripts/lab-vivo.ps1`). Se pausa en cada apertura y se reanuda tras una caída (apartado 11). |
| «Usando todo lo que tenemos: noticias, todas las bolsas, gráficas, análisis técnico…» | Familias como plugins (apartado 6) sobre velas de 1 min, funding, aperturas y cierres de las bolsas, miedo y codicia, datos macro, noticias e indicadores técnicos causales. Las noticias tienen 91 días de historia; en vivo solo se miden en avance. |
| «No al tuntún: con lógica aplastante, que venga de algo que funcione hoy o que tenga mucho sentido» | Cada hipótesis lleva mecanismo, fuente, predicción y falsación (apartados 5 y 15). Tiene como mucho 6 variantes fijadas antes de medir y 3 filtros. Nada se repite sin una idea nueva. El generador no mide nada por su cuenta. |
| «Muchas estrategias al día hasta encontrar la de ≥ 1 % diario» | Cupo de 24 hipótesis al día (≤ 144 variantes; 11.4). La meta se informa en R al día y en % de la cuenta con 0,5 % de riesgo. El riesgo no se sube (2.4). |

Reglas del usuario (intocables, `config.USER_RULES`):
- Stop ≥ 1,5 %.
- Toda parte que gana (objetivo, salida parcial o suelo del cerrojo) ≥ 2R y ≥ 3 % neto.
- Sin break-even ni nada que mueva el stop antes de la ganancia mínima.
- Largos al contado y cortos en perpetuo.
- Una posición por moneda; ≤ 6 entradas y ≤ 3 % de riesgo al día.
- Historia de ≤ 3 años.

Se aplican a todo lo que pueda llegar a la cartera. Una variante fuera de reglas solo se prueba como información: va
marcada «fuera de reglas» y nunca puede pasar a la cartera (5.4).

## 2. Qué esperar (honestidad con los datos)

### 2.1 El 1 % diario, en R

Con 0,5 % de riesgo por operación, 1 R es el 0,5 % de la cuenta. El 1 % al día son 2 R por día natural:

| Operaciones al día | R medio necesario por operación |
|---|---|
| 1 | 2,00 |
| 2 | 1,00 |
| 3 | 0,67 |
| 4 | 0,50 |
| 6 (el tope del día) | 0,33 |

Lo medido hasta hoy, con los mismos costes y reglas (`docs/05`):

| Estrategia | Operaciones | R medio fuera de muestra | Estado |
|---|---|---|---|
| «6 diarias v2» | 4,8 por jornada | −0,075 (≈ −0,18 % por jornada) | pierde |
| E2 | ≈ 0,5 al día | +0,14 | descartada por el usuario el 29-10-2026 («poco beneficio, pocas operaciones»); no se vuelve a proponer |
| Ruptura V3 | 0,39 por jornada | +0,43 (−0,03 dentro; +0,16 en 3 años) | frente a su base, p = 0,09: no es ventaja demostrada |

Con las reglas del usuario y 0,5 % de riesgo (docs/05, sección 19):
- Sabiendo hacia dónde estará el precio 24 h después el 80 % de las veces, se ganaría ≈ +0,9 a +1,0 % al día.
- Sabiéndolo siempre, de +1,5 a +1,96 % al día.

Un 1 % diario equivale a acertar el sentido ≈ 80 % de las veces en cada apertura. Ninguna señal medida pasa del 49 %.

### 2.2 La trampa de probar muchas

**Por qué es una trampa.** Probar muchas estrategias multiplica los falsos positivos. El laboratorio de 2.000 lo
demostró: 68 de 1.358 batían a su base, exactamente lo que tocaba por azar. Por pura suerte, la mejor de miles de
estrategias sin ventaja puede dar entre +0,15 y +0,3 % al día fuera de muestra. La cifra sale con ≈ 3.500 pruebas
efectivas y 14 meses de fuera de muestra. Con 1 operación al día sale la parte baja; con 4, la alta.

**Cuánto sube el listón.** El Sharpe deflactado (Bailey y López de Prado, 2014) exige superar el máximo esperado por
azar entre N pruebas. Ese máximo, en errores típicos, crece despacio:

| N pruebas efectivas | 100 | 1.000 | 3.500 | 10.000 | 50.000 | 100.000 |
|---|---|---|---|---|---|---|
| Máximo esperado por azar (z) | 2,53 | 3,26 | 3,60 | 3,86 | 4,24 | 4,39 |

El fuera de muestra tiene ≈ 440 días naturales. Para que el Sharpe deflactado llegue a 0,95 hace falta un Sharpe diario
de ≈ 0,25 con N = 3.500 y de ≈ 0,28 con N = 50.000.

### 2.3 Lo que sí pasaría

Los filtros no frenan un 1 % diario de verdad. Un ejemplo: 4 operaciones al día de +0,5 R con 1,9 R de desviación.
- Su Sharpe diario es ≈ 0,5: unas 11 desviaciones en 440 días, frente a las ≈ 5,2 que pide el listón.
- En la prueba en avance se notaría en pocas semanas.

Lo que frenan es confundir suerte con ventaja. Una ventaja pequeña de verdad no se separa de la suerte con estos datos:
por ejemplo, +0,1 R con 2 operaciones al día (+0,1 % diario). Solo una prueba en avance larga podría separarla.

### 2.4 Por qué el 1 % es una meta y no un filtro

- **Ganarían las afortunadas.** Usar el 1 % como filtro premia la cola de la suerte: la mejor de miles siempre parece
  mejor de lo que es (la «maldición del ganador»). En avance baja.
- **Invita a retocar.** Lo que casi llega tienta a ajustar un parámetro más. Es el «jardín de senderos que se bifurcan»:
  se acaba sobreajustando hasta cruzar la línea.
- **Se perderían ventajas reales.** Una ventaja robusta de +0,3 % diario se tiraría.

Por eso el 1 % se calcula y se enseña en cada candidata («le faltan 1,4 R al día: 0,7 % de la cuenta»). Ningún criterio
lo usa para aprobar o descartar. El riesgo por operación sigue siendo `config.RISK_PER_TRADE_PCT` (0,5 %). No se informa
nada «con apalancamiento». En la simulación, los perpetuos no pasan de 2 × el capital y la exposición no pasa del 100 %,
como en el paper.

## 3. Arquitectura

```
 Agente generador (Claude, 4 pasadas al día)        Usuario (también puede añadir hipótesis)
        │  escribe hipótesis JSON (y, si hace falta, una familia nueva con sus pruebas)
        ▼
 lab_vivo\cola\entrada\*.json ──►  CORREDOR  (python -m bot.lab_vivo correr; proceso aparte, prioridad baja,
                                     │        afinidad a 3 CPU, memoria limitada, pausa en aperturas)
                                     │  admisión → rápida → completa → robustez → avance
                                     │  cada trabajo en su subproceso (1 a 3 a la vez)
                                     ▼
                         lab_vivo.db  (todas las pruebas; solo se añade)
                                     │
   estado.json · mejores.json · cola.json · diario\ · hipotesis\ · para_el_generador.json · propuestas\
                    │                                                  │
   servidor del bot (solo lee archivos): GET /api/lab_vivo,   evolución: lee propuestas\ → pestaña
   mensaje «lab_vivo», pestaña Laboratorio                    Evolución → decide el usuario → decisiones\
```

**Procesos:**

| Proceso | Qué hace |
|---|---|
| Corredor | `python -X utf8 -m bot.lab_vivo correr`. Supervisor ligero: cola, pausas, recursos, ingesta de resultados y salidas JSON. Es el **único que escribe** en `lab_vivo.db`. |
| Tareas | `python -X utf8 -m bot.lab_vivo tarea <id>`. Un subproceso por trabajo. Lee su encargo, calcula y escribe su resultado en un archivo, que ingiere el corredor. Muere con el corredor (Job Object, 11.6). |
| Vigilante | `scripts/lab-vivo.ps1` (apartado 16). Relanza el corredor si cae o se cuelga. |
| Generador | Tarea programada de Claude en el PC del usuario (apartado 15). |
| Bot | Sin cambios. Su vigilancia mantiene al día la caché de 1 min (`HIST_DIR/1m/<SIM>-1m-<AAAA-MM>-api.json`) y el funding (`HIST_DIR/funding`), que el laboratorio lee para la prueba en avance. |

**Módulos del paquete `bot/lab_vivo/`:**

| Módulo | Qué hace |
|---|---|
| `__init__.py`, `__main__.py` | Versión y órdenes (11.10) |
| `ajustes.py` | Variables `BOT_LAB_VIVO_*`, rutas y límites (11.10) |
| `criterios.py` | Criterios v1 (apartados 8 y 10) con su fecha y su sha |
| `almacen.py` | `lab_vivo.db` (apartado 12) |
| `datos.py` | `Mercado` y caché por época (4 y 6.2) |
| `indicadores.py`, `filtros.py` | Indicadores causales y filtros comunes (6.3) |
| `salidas.py` | Catálogo de salidas (6.5) |
| `libro.py` | La gestión verificada del paper (apartado 7) |
| `medidas.py` | Estadística, robustez y meta (8, 9 y 10.3) |
| `hipotesis.py` | Formato, firma, reglas y `a_params_motor` (5 y 10.4) |
| `etapas.py` | Lo que calcula cada trabajo (apartado 8) |
| `agenda.py` | Pausas en las aperturas (11.5) |
| `recursos.py` | Prioridad, afinidad, Job Object, memoria y CPU (11.6) |
| `corredor.py` | El supervisor (apartado 11) |
| `salida_web.py` | Salidas JSON (apartado 13) |
| `canario.py` | Reproducción de cifras conocidas (7.6) |
| `familias/` | `base.py` y una familia por archivo (apartado 6) |

**Qué reutiliza (no reinventa):**

| De | Qué |
|---|---|
| `bot/laboratorio.py` | `month_files`, `parse_zip`, `parse_api`, `dense_from_rows` y `load_dense` (velas de 1 min). `load_funding`, `load_fng` y `load_macro`. `special_session` y `split_point`. Estadística: `hac_t`, `t_sf`, `bh_qvalues`, `deflated_sharpe`, `expected_max_z`, `effective_trials` y `moments`. `Lab` con `events` y `candidates`, para el adaptador del laboratorio (6.6). `btc_trend` y `funding_features`. |
| `bot/backtest_aperturas.py` | `index_data` y `build_openings` (aperturas reales con festivos), `SYMS`, `zip_path`, `part_path` y `day_stats`. |
| `bot/apertura.py` | `openings_on` y `LAB_WINDOWS` (agenda y pausas), `impulse_params`, `impulse_plan`, `simulate_plan`, `impulse_instrument`, `day_limits`, `expected_funding_pct`, `ImpulseRun` y los presets `SEIS2` y `PRESETS`. |
| `bot/paper.py` | `exit_rules`, `lock_update`, `stop_loss_frac`, `funding_pay`, `cost_rt_pct`, `rule_win_ok` y las constantes de los controles del paper. |
| `bot/comisiones.py` | Perfiles (`okx_eu_con_xperps` por defecto y `okx_eu_sin_xperps`), `tasa`, `coste_rt_pct`, `hay_cortos`, `elegir` y `costes_de_backtest` (canario). |
| `bot/evolucion.py` | `cumple_reglas`, `bootstrap_mejora`, `t_robusta` y `contraido` (prueba en avance, como el campeón frente a los aspirantes). |
| `bot/vigilancia.py` | `load_cache` (velas de la caché con el mes en curso) y `next_midnight`. |
| `bot/backtest.py` | `known` y `CLOSE_MARGIN_MS` (un cierre de bolsa se conoce 15 min después). |
| `BACKTEST_DIR/controles/selectiva/medir_selectiva.py` | El libro verificado: `Cartera`, `Salida`, `simular`, `Book`, `base_azar`, `r_opuesto`, `estres`, `ic_dias`, `cluster_se`, `bloque` y `capital`. Es la réplica D de `replica_seis.py` y `salidas_motor.py`. Se **porta** a `bot/lab_vivo/libro.py` y `medidas.py` sin cambiar su lógica (apartado 7). |

**Nunca toca:**
- `bot.db`, `estrategia_activa.json`, la cartera paper, el proceso del bot ni su configuración.
- `HIST_DIR` y `BACKTEST_DIR`: solo los lee.
- La red. La única excepción es la fase 2, que lee `GET /api/noticias` del propio bot en `127.0.0.1`.
- Los archivos que están editando otros flujos.
- No añade dependencias: solo la biblioteca estándar y numpy (`docs/04`, 0).

## 4. Datos, periodo y causalidad

### 4.1 Fuentes

| Clave | Archivo | Cobertura (29-10-2026) | Se conoce… | Uso |
|---|---|---|---|---|
| `velas_1m` | `HIST_DIR/1m/<SIM>-1m-<AAAA-MM>.zip` y `-api.json` (BTC, ETH, SOL y XRP en USDT de Binance al contado: la aproximación al USDC de OKX EU) | 2021-01 → hoy. El mes en curso lo añade la vigilancia del bot. | al cerrar cada vela (t + 1 min) | todo |
| `funding` | `HIST_DIR/funding/<SIM>USDT.json` (Binance USDT-M, cada 8 h) | 2021-01-01 → hoy (la vigilancia lo refresca cada 4 h) | al liquidar (sello de la fila) | cortos en perpetuo, filtros |
| `aperturas` | `apertura.openings_on`, `backtest_aperturas.build_openings` (festivos del histórico de índices) y `laboratorio.special_session`, con `apertura.LAB_WINDOWS` | calendario | de antemano | familias de apertura y pausas |
| `bolsas` | `HIST_DIR/indices/*.json` (cierres diarios de las 9 bolsas) | 2016-10 → hoy | cierre + 15 min (`backtest.known`) | bolsas previas |
| `fng` | `HIST_DIR/sentimiento/fng.json` | 2018-02 → 26-10-2026. No se actualiza solo: lo baja `historia_extra`. | 01:00 UTC del día (`historia_extra.fng_known_at`) | filtros |
| `macro` | `HIST_DIR/macro/eventos.json` (IPC, empleo, FOMC) | 2021 → 2026 | la hora, de antemano; su reacción, 15 min después | familias macro |
| `noticias_historia` | `historia_noticias/noticias.jsonl` (48.272 titulares de 22 medios) más los sentidos puestos por Claude a ciegas (787, `controles/fuentes/`) | 29-07 → 27-10-2026 | su hora de publicación | solo exploratoria: los 91 días caen dentro del fuera de muestra |
| `noticias_3a` | `noticias_historia/lab/noticias.npz` y `meta.json` (`bot/lab_vivo/noticias.py`): historias de GDELT GKG y de 24 medios de cripto, deduplicadas como la mesa, en racimo y con el triaje de la IA local del bot (apartado [21](#21-noticias-de-3-años-29-10-2026)) | 01-07-2023 → 28-10-2026 cuando acabe el triaje (hasta entonces no hay archivo) | desde el minuto siguiente al sello del medio que completa el racimo | familias de noticias con historia (`noticia_racimo`, `noticia_apertura`) |
| `noticias_vivas` | `GET /api/noticias` del bot (fase 2) | solo desde que se guarde | al captarse | solo prueba en avance |

Sin historia útil para el backtest:
- GDELT DOC 2.0 (las series de tono y volumen de `historia_extra`): solo 2021-01 → 2022-03. Las noticias de 3 años
  salen de los archivos GKG de GDELT 2.0, que sí cubren el periodo (`noticias_3a`).
- Ratio largo/corto: desde el 23-09-2026.

Una familia que los necesite solo puede ser **exploratoria** (8.6). La admisión rechaza una fuente que cubra menos del 50
% del periodo, como `laboratorio.SOURCE_COVER_MIN`.

### 4.2 Época, periodo y corte

- **Época** = mes.
  - Periodo de la época «2026-11»: del 01-11-2023 al 31-10-2026. Son los 3 años hasta el último día del mes anterior:
    regla de los 3 años como mucho.
  - Si el corredor arranca antes del 01-11-2026, la primera época es la «2026-10»: del 01-10-2023 al 30-09-2026.
  - Antes del inicio se cargan los días de calentamiento que pida cada familia: solo para indicadores, sin operaciones.
- **Corte fijo: 16-08-2025** (medianoche de Madrid).
  - Dentro de muestra, antes del corte; fuera de muestra, desde el corte. Es el mismo corte que el laboratorio,
    `impulso_7d.json`, `seis2_resumen.json` y las selectivas, así que las cifras se comparan.
  - Al avanzar las épocas, la muestra encoge. Cuando baje de 12 meses (hacia agosto de 2027), el usuario decide si se
    mueve el corte.
- **Ojo: el fuera de muestra histórico ya está usado.** Lo han mirado muchos estudios (9.2). Por eso cuenta en N y por
  eso hace falta la prueba en avance, que es lo único virgen.
- **Cambio de época:** el día 1 de cada mes a las 05:00 de Madrid.
  - Se rehace la caché y se pasa el canario (7.6).
  - Se vuelven a medir las candidatas, las que están en avance y las listas. Es una actualización: no cuenta como prueba
    nueva.
  - Lo descartado se queda como estaba, con su época.

### 4.3 Caché de datos

| Caché | Contenido | Uso |
|---|---|---|
| `lab_vivo\cache\<época>-<firma>\` | Por moneda, en `.npy` de float64 / int64: **en bruto** (`t`, `o`, `h`, `l`, `c`, `v`: solo los minutos con vela, como `A[c]` de `medir_selectiva.py`) y **en rejilla densa** (`laboratorio.dense_from_rows`: `t0`, `n`, `o`, `h`, `l`, `c`, `v`, `qv`, `valid`). | El libro usa las velas en bruto (para reproducir las cifras verificadas). Las familias usan la rejilla densa (ventanas por número de velas). |
| `lab_vivo\cache\vivo\` | Los últimos 60 días, refrescados cada hora con `vigilancia.load_cache`. | Prueba en avance. |

- Las tareas los abren con `np.load(..., mmap_mode="r")`. Así varios subprocesos comparten las páginas del sistema y no
  copian nada (memoria justa, 11.6).
- La caché se construye moneda a moneda, para no tener las cuatro en memoria a la vez.
- **Firma de datos:** sha1 de los nombres, tamaños y fechas de los archivos usados. Va en cada resultado. Si cambia (p.
  ej., llega el zip oficial de un mes que era `-api.json`), se vuelve a pasar el canario.

### 4.4 Sin mirar al futuro (reglas para toda familia)

- **Velas:**
  - Una vela de 1 min que abre en t se conoce en t + 1 min.
  - Una barra de N min se conoce en su cierre (`t_cierre`).
  - Las comparaciones de ruptura van contra el máximo o el mínimo de las barras **anteriores**: la actual no entra.
- **Diario:** solo el último día UTC **completo**, como `laboratorio.btc_trend`.
- **Pivotes y giros:** un máximo o mínimo de p barras a cada lado se conoce p barras después.
- **Percentiles y medias:** solo con ventanas hacia atrás.
- **Funding:** la última tasa con sello ≤ t, como `laboratorio.funding_features`.
- **Miedo y codicia:** con 1 h de retraso. **Bolsas:** cierre + 15 min. **Macro:** la reacción, 15 min después de la
  publicación.
- **Lo comprueba la máquina:** la admisión hace la **prueba del recorte** a cada familia y variante (8.1, A8;
  `bot/lab_vivo/recorte.py`). Una señal con decisión t solo puede usar lo que se conoce en t: las velas que abren antes
  de t, las barras con `t_cierre ≤ t` y el funding, el miedo y codicia y las bolsas con sello ≤ t (`Mercado.recortado(t +
  1 ms)`).
  - **Corte en cada señal** (todas, o 60 por tramo): las señales con t' ≤ tᵢ, calculadas solo con los datos hasta tᵢ,
    tienen que salir idénticas a las calculadas con todos. Caza **con certeza** cualquier mirada al futuro de 1 min o
    más en la señal cortada.
  - **Cortes al azar** (20, semilla fija): las señales con t' ≤ T, idénticas con los datos hasta T y con todos. Cazan
    también lo que una mirada al futuro **quita** (una señal que no sale porque se ve lo que viene), cuando el corte cae
    en ella: eso no se caza siempre.
  - Hasta la revisión del 29-10-2026 solo había cortes al azar y solo se comparaban las señales con t' < T: una familia
    que decidía en t con la vela que abre en t (1 min de futuro) no se veía nunca (20.8).

## 5. Hipótesis (formato)

### 5.1 Dónde

| Carpeta | Qué hay |
|---|---|
| `lab_vivo\cola\entrada\<id>.json` | Lo que escriben el generador o el usuario. Se escribe primero `<id>.json.tmp` y luego se renombra. |
| `lab_vivo\cola\aceptadas\` | El corredor mueve aquí lo admitido. |
| `lab_vivo\cola\rechazadas\` | Lo rechazado, con `admision.motivos` añadido. |

La verdad está en `lab_vivo.db`. Las carpetas son el buzón y su copia legible.

### 5.2 Ejemplo completo

```json
{
  "formato": 1,
  "id": "h-20261029-1215-ruptura_canal-48y96h",
  "creada": "2026-10-29T12:15:00+01:00",
  "autor": "generador-claude",
  "titulo": "Ruptura del canal de 48 y 96 h en velas de 1 h, 24/7",
  "familia": "ruptura_canal",
  "familia_version": 1,
  "logica": {
    "mecanismo": "Por encima de los máximos de varios días se acumulan stops de cortos y órdenes de compra por ruptura; al romper, su ejecución y las liquidaciones de los apalancados empujan el precio, y la cripto tiende a seguir su tendencia de días (momento de series temporales).",
    "por_que_ahora": "El momento de 1 a 4 semanas está documentado en cripto y el apalancamiento en perpetuos sigue siendo alto: las cascadas de liquidaciones alargan las rupturas.",
    "fuentes": [
      {"tipo": "libro", "cita": "Faith, C. (2007). Way of the Turtle (reglas de ruptura de 20 y 55 días)."},
      {"tipo": "articulo", "cita": "Hurst, Ooi y Pedersen (2017). A Century of Evidence on Trend-Following Investing."},
      {"tipo": "articulo", "cita": "Liu y Tsyvinski (2021). Risks and Returns of Cryptocurrency (momento de series temporales)."}
    ],
    "prediccion": "Ganadoras por debajo del 40 % y pocas operaciones de +4R o más que pagan las pérdidas; mejor con el cerrojo que con la horquilla; 0,5 a 1,5 operaciones al día entre las 4 monedas.",
    "falsacion": "Se descarta si fuera de muestra no bate a su base emparejada (el sentido al azar en las mismas horas y monedas) o si lo que gana depende de 5 operaciones.",
    "parecidas_ya_probadas": ["laboratorio: ruptura del rango de apertura (2.000)", "selectiva: Ruptura V3 (rango de apertura)"],
    "que_aporta": "No es la ruptura del rango de los primeros minutos de una apertura: es la de varios días, a cualquier hora, y deja correr con el cerrojo.",
    "revision": {"plausibilidad": "alta", "sobreajuste": "bajo: 1 filtro como mucho", "muestra": "≈ 0,8 señales al día", "regimen": "debería ir en los dos sentidos; en lateral pierde", "ejecucion": "a mercado en la vela siguiente; SOL y XRP con más deslizamiento (estrés)"}
  },
  "datos": ["velas_1m"],
  "universo": {"monedas": ["BTC", "ETH", "SOL", "XRP"], "momento": "continuo"},
  "variantes": [
    {"id": "V1", "params": {"horas": 48, "tf_min": 60, "stop": "atr", "stop_atr_k": 1.5}, "salida": {"tipo": "A", "sl": "estructural"}},
    {"id": "V2", "params": {"horas": 96, "tf_min": 60, "stop": "atr", "stop_atr_k": 1.5}, "salida": {"tipo": "A", "sl": "estructural"}},
    {"id": "V3", "params": {"horas": 48, "tf_min": 60, "stop": "atr", "stop_atr_k": 1.5, "regimen_btc": "a_favor"}, "salida": {"tipo": "A", "sl": "estructural"}},
    {"id": "V4", "params": {"horas": 96, "tf_min": 60, "stop": "atr", "stop_atr_k": 1.5, "regimen_btc": "a_favor"}, "salida": {"tipo": "A", "sl": "estructural"}},
    {"id": "V5", "params": {"horas": 48, "tf_min": 60, "stop": "atr", "stop_atr_k": 1.5}, "salida": {"tipo": "C", "sl": "estructural", "lock_r": 3, "trail_r": 2}},
    {"id": "V6", "params": {"horas": 96, "tf_min": 60, "stop": "atr", "stop_atr_k": 1.5}, "salida": {"tipo": "C", "sl": "estructural", "lock_r": 3, "trail_r": 2}}
  ],
  "reglas": {"riesgo_pct": 0.5, "max_entradas_dia": 6, "max_riesgo_dia_pct": 3.0, "largos": "spot", "cortos": "perp", "una_por_moneda": true},
  "prioridad": 3,
  "frecuencia_esperada": {"op_dia": 0.8, "como": "estimación del generador"},
  "cumple_reglas": true,
  "fuera_de_reglas": false,
  "exploratoria": false,
  "nota": null
}
```

### 5.3 Campos

| Campo | Obligatorio | Qué es |
|---|---|---|
| `formato` | sí | 1 |
| `id` | sí | `^h-\d{8}-\d{4}-[a-z0-9_-]{3,60}$`, único: fecha y hora de Madrid de la creación y un nombre corto |
| `creada`, `autor` | sí | ISO con zona; `generador-claude`, `usuario` o `coordinador` |
| `titulo` | sí | ≤ 120 caracteres, en español |
| `familia`, `familia_version` | sí | Una familia registrada. La versión tiene que ser la vigente: si no, se rechaza. |
| `logica.mecanismo`, `fuentes` (≥ 1), `prediccion`, `falsacion` | sí | Textos de ≥ 40 caracteres. `fuentes[]` = `{tipo: articulo\|libro\|estrategia_publica\|resultado_propio, cita, url?, nota?}`. Un `resultado_propio` solo puede ser de la prueba en avance, de la cuenta honesta o de lo que el generador ve (la etapa, el código y las cifras de dentro): nunca un corte del fuera de muestra histórico (13.5 y 15.3). |
| `logica.que_aporta` | si hay `parecidas_ya_probadas` | En qué se diferencia de lo probado |
| `logica.revision` | recomendado | La autorrevisión del generador con los criterios de `edge-strategy-reviewer` (15.4) |
| `datos` | sí | Claves de 4.1: tienen que estar en la familia y con cobertura |
| `universo` | sí | `monedas` (⊆ `config.COINS`), `momento` (`aperturas` o `continuo`), `ventanas` (⊆ `LAB_WINDOWS`, solo con aperturas) |
| `variantes` | sí | 1 a 6. `id` V1…V6, `params` del esquema de la familia (lo que falte va por defecto) y `salida` del catálogo (6.5) |
| `reglas` | sí | Tienen que ser exactamente las del usuario y el riesgo, el de `config.RISK_PER_TRADE_PCT`. Otra cosa solo con `fuera_de_reglas: true`. |
| `prioridad` | no | 1 (antes) a 5; 3 por defecto |
| `cumple_reglas` | no | Lo que cree quien la escribe. **El corredor lo recalcula y manda el suyo.** |
| `fuera_de_reglas`, `exploratoria` | no | false por defecto (5.4 y 8.6) |

### 5.4 Firma, duplicados y fuera de reglas

- **Firma de una variante:** sha1 de `{familia, versión, params completos (con los valores por defecto), salida, universo}`
  normalizados, cortado a 16 caracteres.
  - Normalizar: los números, redondeados a 6 decimales; las listas sin orden (monedas, ventanas), ordenadas.
  - Una variante con la firma de otra ya probada no se vuelve a medir: queda «duplicada», con el enlace a su resultado.
  - Si todas las variantes de una hipótesis están duplicadas, se rechaza: «ya probada».
  - Volver a proponer algo descartado exige una versión nueva de la familia o parámetros distintos, y `que_aporta`.
    Cuenta como prueba nueva.
- **Fuera de reglas** (`fuera_de_reglas: true`):
  - Se mide en su tarea con `config.USER_RULES` apagado solo para esa variante.
  - Va marcada en todas las salidas y **cuenta en N**.
  - Nunca puede ser candidata, ni estar en avance ni proponerse.
  - Sin la marca, una variante que no cumpla las reglas se rechaza.

## 6. Familias (API de plugins)

Una **familia** es un archivo de Python en `bot/lab_vivo/familias/<nombre>.py`. A partir de las velas (y del funding,
las aperturas, etc.) da las **señales de entrada** sin mirar al futuro. Las salidas no son suyas: las pone el libro, con
la gestión verificada del paper y una salida del catálogo (6.5). Así ninguna familia puede inventarse una salida que
rompa las reglas.

### 6.1 Contrato (`bot/lab_vivo/familias/base.py`)

```python
@dataclass(frozen=True)
class Param:
    tipo: str                      # "int" | "float" | "str" | "bool" | "lista"
    defecto: object
    opciones: tuple | None = None  # valores permitidos (si no, min y max)
    min: float | None = None
    max: float | None = None
    filtro: bool = False           # cuenta para el tope de 3 filtros cuando no está en su valor neutro
    neutro: object = None          # el valor que lo apaga ("ninguno", "todos", 0…)
    texto: str = ""                # para describir la variante en español

class Senales:
    """Arrays de numpy de la misma longitud, ordenados por t."""
    t: np.ndarray          # int64, ms UTC, múltiplo de 60.000: instante de la decisión (cierre de la última vela usada)
    moneda: np.ndarray     # int8, índice en Mercado.monedas
    dir: np.ndarray        # int8: +1 largo (contado), −1 corto (perpetuo)
    stop_pct: np.ndarray   # float64: distancia del stop en % (NaN = la de la salida de la variante)
    ventana: np.ndarray    # int8: índice en apertura.LAB_WINDOWS, o −1 si es continua
    clave: np.ndarray      # int32 libre (tipo de señal), para el diagnóstico
    @staticmethod
    def juntar(partes) -> "Senales": ...
    def entre(self, desde_ms, hasta_ms) -> "Senales": ...

class Familia:
    nombre: str                    # = nombre del archivo
    version: int                   # sube con CUALQUIER cambio del archivo (11.8)
    titulo: str
    descripcion: str
    momento: str                   # "aperturas" | "continuo"
    datos: tuple[str, ...]         # claves de 4.1
    historia: bool                 # False: solo se puede medir en avance (exploratoria)
    calentamiento_d: int           # días de datos antes del inicio del periodo
    parametros: dict[str, Param]
    max_senales_dia: float = 50.0  # salvaguarda: más es un error (señales por nivel en vez de por flanco)

    def senales(self, m: "Mercado", p: dict, desde_ms: int, hasta_ms: int) -> Senales: ...
    def describir(self, p: dict) -> str: ...   # la regla en español llano (web y propuesta)
```

**Reglas de las señales:**
- **Una por flanco.** La señal sale cuando la condición **empieza** a cumplirse, no en cada minuto que dura.
- **Entrada:** la primera vela de 1 min que abre en `t` o después, a mercado y con el deslizamiento (7.2).
- **Moneda ocupada:** si la moneda está ocupada o el día no deja entrar, la señal se pierde (veto apuntado). No espera.
- **Stop por señal:** `stop_pct` permite un stop estructural (al otro lado del rango, k × ATR…).
  - El libro lo sube a 1,5 % si es menor (regla del usuario).
  - Si pasa del 5 %, la señal no entra (veto `stop_lejano`: un objetivo de 3R sería del 15 %).
- **Prohibido:** la red y escribir archivos.
  - La tarea pone una guarda que hace fallar `socket.socket` antes de importar las familias.
  - Las pruebas lo comprueban.

### 6.2 El mercado que ve una familia (`bot/lab_vivo/datos.py`)

```python
class Mercado:
    monedas: tuple[str, ...]                 # config.COINS
    periodo: dict                            # {"inicio", "fin", "corte"} en ms (Madrid) de la época
    def velas(self, moneda) -> Velas         # en bruto: t, o, h, l, c, v (solo los minutos con vela)
    def denso(self, moneda) -> Denso         # rejilla de 1 min sin huecos (dense_from_rows): t0, n, o, h, l, c, v, qv, valid
    def barras(self, moneda, tf_min) -> Barras   # agregadas desde la densa y cerradas: t, t_cierre, o, h, l, c, v, n_validas
    def funding(self, moneda) -> tuple[np.ndarray, np.ndarray]   # (t_ms, tasa por periodo en fracción)
    def tasa_en(self, moneda, t_ms) -> float | None              # última publicada antes de t, en %
    aperturas: list[dict]                    # build_openings de LAB_WINDOWS sin las sesiones especiales
    def bolsas_conocidas(self, t_ms) -> list[tuple]              # (bolsa, día, retorno %) cerradas hace ≥ 15 min
    def fng_en(self, t_ms) -> float | None
    macro: tuple[np.ndarray, list[str]]      # calendario (las horas se saben de antemano)
    noticias: "Noticias | None"              # noticias_3a con t < hasta (None sin el archivo compilado; apartado 21)
    def recortado(self, t_ms) -> "Mercado"   # la misma vista sin nada posterior a t (prueba del recorte)
```

`recortado` corta las velas, las barras, el funding, el miedo y codicia, los cierres de bolsa y las noticias. Deja el
calendario de aperturas y el de datos macro (solo horas), que se conocen de antemano.

### 6.3 Indicadores y filtros comunes

**`bot/lab_vivo/indicadores.py`: funciones causales con numpy.** Cada valor i se conoce al cierre de la barra i.
- EMA, SMA, ATR, RSI y ADX de Wilder.
- Bandas de Bollinger y su anchura.
- Máximos y mínimos de N barras anteriores (sin la actual).
- Percentil en ventana hacia atrás.
- Pivotes confirmados (con su retraso).
- VWAP desde un ancla.
- Volumen relativo frente a sesiones anteriores.

Se portan, con sus pruebas, de `medir_selectiva.py` (`f_ema`, `f_wilder`, `f_atr_adx`, `r_adx`, `m_pivotes`…) y de
`laboratorio.coin_tables` (ATR de 14 h en percentil de 30 días).

**`bot/lab_vivo/filtros.py`: filtros que cualquier familia declara como parámetros.** Cada uno cuenta como 1 filtro.

| Filtro | Valores | Como |
|---|---|---|
| `regimen_btc` | `ninguno` · `a_favor` · `no_contra` | Cierre diario de BTC frente a sus medias de 20 y 50 días (`laboratorio.btc_trend`) |
| `volatilidad` | `todos` · `no_baja` · `alta` · `baja` | ATR de 14 h en percentil de 30 días (como el laboratorio) |
| `funding` | `todos` · `no_extremo_en_contra` | Percentil de 30 días; no entra si el extremo va contra la operación |
| `dias` | `todos` · `mar-jue` · `sin_fin_de_semana` | Día de Madrid |
| `aperturas` | `todas_horas` · `solo_en_apertura` · `fuera_de_apertura` | Si la decisión cae entre la apertura y +120 min de una ventana |
| `noticias_en_contra` | `no` · `veto` | Solo en vivo (exploratoria): veto de una noticia fiable en contra, como «6 diarias» |
| `noticias_previas` | `todas` · `no_contra` · `a_favor` | Noticias de 3 años (`noticias_3a`): el signo de la suma de los sentidos de las historias de esa moneda (y de las generales, con `noticias_generales` «todas») de las `noticias_horas` {4, 8, 12} antes de la decisión, con importancia ≥ `noticias_imp_min` {0, 5, 7} (que también cuenta como filtro); «no_contra» no entra si van en contra y «a_favor» solo entra si van a favor, como `news_mode` del motor (21.9) |

### 6.4 Prueba de conformidad (toda familia, siempre)

`tests/test_lab_vivo_familias.py` la pasa a todas las familias registradas, y la admisión la repite con datos reales
(A8):
1. **Esquema.** Arrays de la misma longitud; `t` múltiplo de 60.000 y ordenado; `dir` ∈ {−1, +1}; `moneda` en rango;
   `stop_pct` NaN o > 0.
2. **Recorte** (`recorte.py`, 4.4). Sobre dos tramos de 60 días, con el calentamiento de la familia **y de sus
   filtros** (el régimen cripto pide 430 días; sin ellos, sus variantes no daban ninguna señal en los tramos y la prueba
   no probaba nada): un corte en cada señal (60 por tramo en la admisión; 5 en las pruebas sintéticas) y 20 cortes al
   azar. Las señales con `t ≤ T` son idénticas con los datos hasta T y con todos. `tests/test_lab_vivo_recorte.py`
   comprueba que caza familias trampa que miran 1 min, 15 min y 1 h al futuro.
3. **Determinismo.** Dos pasadas iguales.
4. **Por flanco.** Menos de `max_senales_dia` señales por día y como mucho 1 por moneda y minuto.
5. **Velocidad.** ≤ 2 s con 90 días sintéticos; en la admisión, ≤ 60 s con los 3 años.
6. **Sin red ni disco.** Con `socket` bloqueado y una carpeta de solo lectura.

### 6.5 Catálogo de salidas (fijado; cumplen las reglas salvo el cierre por tiempo)

La salida es de la variante, no de la familia. Se convierte en parámetros de `apertura.impulse_plan` sobre la base de
`apertura.SEIS2`: largos al contado, cortos en perpetuo, 6 entradas y 3 % de riesgo al día y ≥ 3 % neto. Se comprueba
con `evolucion.cumple_reglas`: stop ≥ 1,5 %, toda parte que gana ≥ 2R y ≥ 3 % neto y nada que mueva el stop antes.

**El cierre por tiempo no cumple «o gano más de 2 o pierdo 1».** Todas las salidas del catálogo (A, H, C y OBJ) cierran
por tiempo **antes** de la ganancia mínima (`max_hold_h` sin activar, o el `fin_ms` de la señal). Esa parte puede salir
entre −1R y +2R, y si gana, con menos de 2R y del 3 % neto. Es la excepción que el usuario solo ha aceptado en «6
diarias» (CLAUDE.md). Lo que cierra por tiempo con el cerrojo activado sí cumple, porque sale por encima de su suelo.
- **Cuánto pesa** (medido por la revisión en la primera tanda): `h06-keltner_1h:V3`, 333 de 898 operaciones cierran por
  tiempo, 320 entre −1R y +2R y 229 ganando menos de 2R; `tendencia_diaria-siembra:V1`, 143 de 399, 134 y 93.
- **Qué se hace.** Se mide en cada tramo (`medidas.cierres_por_tiempo`: las que cierran por tiempo sin cerrojo, las que
  salen entre −1R y +2R y las que ganan menos de 2R). La tarjeta lo lleva en `reglas.excepcion_tiempo` (13.3). Una
  propuesta con esa excepción solo se acepta si la decisión trae `acepta_excepcion_tiempo: true` (13.6): el usuario la
  acepta a sabiendas, como en «6 diarias». Los criterios v1 no cambian.
- **Pendiente de decidir por el usuario:** si quiere salidas sin cierre por tiempo antes del cerrojo (las únicas que
  cumplirían del todo). Ampliar el catálogo es un cambio de diseño: no se hace sin su visto bueno.

| Tipo | Qué es | Parámetros que se pueden fijar | Topes de tiempo |
|---|---|---|---|
| `A` | La de «6 diarias v2» (docs/05, sección 21). La mitad sale en 3R. La otra mitad, al llegar a +3R, sube su stop al suelo y desde ahí lo sigue. | `trail_r` ∈ {1,5; **2**; 3}; `suelo` ∈ {**F**, 2R} | `max_hold_h` ∈ {**24**, 72} sin activar; 168 h activada |
| `H` | Horquilla pura: objetivo fijo, sin parcial ni cerrojo | `target_r` ∈ {2; 2,5; **3**; 4; 6} | `max_hold_h` ∈ {24, 72, 168} |
| `C` | Cerrojo y seguimiento sin parcial (la F1 de `salidas_motor.py`): a +`lock_r` el stop sube al suelo F y sigue a `trail_r` (o a `trail_atr` × ATR de 1 h) | `lock_r` ∈ {2,5; **3**; 4; 5}; `trail_r` ∈ {1; 1,5; **2**; 3} o `trail_atr` ∈ {3, 6} | `max_hold_h` ∈ {24, **72**} sin activar; 168 h activada |

- **Stop** (`sl`): un número fijo entre 1,5 y 5 (%), o `estructural`, que es el `stop_pct` de cada señal.
- **R** es el stop de **esa** operación. Con stops distintos, el tamaño por riesgo mantiene el 0,5 %.
- **Suelo F** = máx(2R; (3 % + coste de ida y vuelta + funding esperado hasta 168 h) / stop), por operación, como la
  Salida «F» de `medir_selectiva.py`. Con un stop de 1,5 % el suelo sube a ≈ 2,16R, para que deje 3 % neto.
- **`avg_hold_h`** (funding esperado): A 16; H 13, 36 o 65 según su tope; C 24. Son las duraciones medidas en las
  réplicas.
- **Admisión:** si `impulse_plan` no da un plan válido en los sentidos que opera la variante con ningún stop posible, se
  rechaza: «la salida no deja 3 % neto con ese stop». Ejemplo: H con 2R y stop fijo de 1,5 %, que deja 2,76 % neto.
  - Con un stop fijo, se mira ese stop.
  - Con uno estructural, de 1,5 a 5 %. Las operaciones cuyo plan no valga se vetan una a una (veto «plan»).

### 6.6 Familias iniciales

**Fase 1:**

| Familia | Momento | Señal | Parámetros (los filtros cuentan para el tope de 3) | Fuente |
|---|---|---|---|---|
| `apertura_impulso` | aperturas | `apertura.ImpulseRun` tal cual, la entrada del motor: impulso de los primeros N min y entrada t:N, pb o brk | `measure_min` {15, 30, 60}; `min_move_pct` [0, 2]; `entry` {t:15, t:30, t:60, pb, brk}; filtros `rvol_min` {—, 1,5; 2}, `vol_filter` y `btc_filter`; `windows`; `coins`. `news_mode` fijo en «etiqueta»: la historia no tiene noticias (como `salidas_motor.py`). | La operativa del usuario. Es la **referencia** y la base del canario (7.6). Lo que pase es operable por el motor sin código nuevo (10.4). |
| `ruptura_canal` | continuo | Cierre de una barra de `tf_min` por encima del máximo (o por debajo del mínimo) de las barras de las `horas` anteriores, por flanco | `horas` {24, 48, 96, 168}; `tf_min` {15, 60}; `lados` {ambos, largos, cortos}; `stop` {atr, canal} con `stop_atr_k` {1; 1,5; 2} (k × ATR(14) de 1 h en %) | Reglas de las «tortugas» (Faith, 2007); seguimiento de tendencia (Hurst, Ooi y Pedersen, 2017) |
| `compresion_ruptura` | continuo | El rango de las últimas `ventana_h` está en el `percentil` más bajo de los 30 días previos. Luego, ruptura de ese rango al cierre de una barra antes de `caduca_h`. | `ventana_h` {12, 24, 48}; `percentil` {0,1; 0,2}; `tf_min` {15, 60}; `caduca_h` {12, 24}; stop al otro lado del rango | Crabel (1990), rangos estrechos (NR4/NR7) y ruptura; Bollinger (2001), compresión de las bandas |
| `tendencia_diaria` | continuo (decide a las 00:00 UTC) | Momento de series temporales. Largo si el retorno de `retorno_d` días > `umbral_pct` y el precio está por encima de su `media`; corto al revés. Una sola entrada mientras siga la señal. | `retorno_d` {7, 14, 28}; `umbral_pct` {0, 2, 5}; filtro `media` {ninguna, ema50, ema200}; `lados` {ambos, largos} | Moskowitz, Ooi y Pedersen (2012); Liu y Tsyvinski (2021) |

**Fase 2** (las programa el implementador o el generador, con sus pruebas):
- `laboratorio`, el adaptador de la gramática de los 2.000:
  - Parámetros: `src`, `conf`, `vol`, `btc`, `win`, `coins`, `dia` y `entry` de `laboratorio.skey`. Faltan el SL, el R y
    la duración, que pone la salida del catálogo.
  - Entradas: las de `Lab.events` y `Lab.candidates` (`slot_t`, y `slot_dec` como decisión; el retesteo, como orden límite
    al borde del rango).
  - El `Lab` de la época se construye una vez (`laboratorio.build_lab`) y sus tablas se guardan para leerlas sin copiarlas.
  - Así se reaprovechan, con la salida A y los costes reales, todas las fuentes verificadas del laboratorio: bolsas en
    cadena, ventana anterior, momento previo, consenso, miedo y codicia, funding, macro…
- `lider_btc`: tras un movimiento fuerte de BTC (≥ k × ATR en 1 h), la alt que aún no se ha movido, en su sentido.
- `reversion_capitulacion`: rebote tras un desplome con volumen extremo y funding negativo; solo largos al contado.
- `funding_extremo`: posicionamiento apalancado extremo con confirmación de precio.
- `tendencia_retroceso`: retroceso dentro de una tendencia diaria y reanudación.
- `apertura_ruptura3`: la Ruptura V3 del motor (`RupturaRun`), cuando el otro flujo termine de programarla.
- `noticias_vivas`, exploratoria (8.6).

### 6.7 Siembra inicial (prerregistrada hoy, 29-10-2026, sin ver ningún resultado)

Para que el corredor tenga trabajo antes de la primera pasada del generador, el implementador mete estas hipótesis. Si al
programar una familia hay que cambiar algo de su regla, se cambia **antes** de medir nada y se apunta en `eventos`.

| Id | Familia | Variantes (todas con las reglas del usuario) |
|---|---|---|
| `h-20261029-0000-apertura_impulso-referencia` | `apertura_impulso` | **V1:** el preset SEIS2 exacto, con salida A y stop de 2 %. Es la **referencia**: no cuenta en N (ya está en las previas) y sirve para comparar con comisiones reales. |
| `h-20261029-0000-ruptura_canal-siembra` | `ruptura_canal` | La del ejemplo de 5.2 (V1 a V6) |
| `h-20261029-0000-compresion_ruptura-siembra` | `compresion_ruptura` | Base: 24 h, p0,2, barras de 60 min, caduca a las 24 h, salida A con stop estructural. **V1:** la base. **V2:** 12 h. **V3:** 48 h. **V4:** p0,1. **V5:** salida C (3R / 2R). **V6:** `regimen_btc` a favor. |
| `h-20261029-0000-tendencia_diaria-siembra` | `tendencia_diaria` | Base: 14 d, umbral 0, ema50, ambos, salida C con stop de 3 %. **V1:** la base. **V2:** 28 d. **V3:** 7 d. **V4:** umbral 5 %. **V5:** solo largos. **V6:** horquilla H de 2R con stop de 5 % y 168 h. |

## 7. El libro: la gestión verificada del paper

`bot/lab_vivo/libro.py` es el libro de `medir_selectiva.py` (réplica D de `replica_seis.py`) portado con su misma
lógica. Lo que cambia está marcado con ⚑.

### 7.1 Qué simula

| Elemento | Cómo |
|---|---|
| Entrada | A mercado, en la apertura de la primera vela en bruto que abre en `t` o después, con 2 pb de deslizamiento (`paper.COST_SLIP_BPS`). La señal vale como mucho hasta esa vela. |
| Plan de salida | `apertura.impulse_plan(d, E, params_de_la_salida, tasa, instrumento)`, que comprueba la regla del 2R y del 3 % neto con el coste del perfil y el funding esperado. Si el plan no vale, la señal no entra (veto «plan»). |
| Gestión | `apertura.simulate_plan(bars, 0, d, plan, fin, slip=..., end_active_ms=...)`. Es la misma gestión que el paper: primero el stop vigente, luego la parcial y el objetivo, y el cerrojo con el extremo de la vela, que vale desde la siguiente. Es pesimista dentro de la vela. |
| Una posición por moneda | También la mitad que queda tras la parcial |
| Topes del día (Madrid) | ≤ 6 entradas y ≤ 3 % de riesgo comprometido (`apertura.day_limits` con los de SEIS2) |
| Controles del paper | Pausa hasta medianoche si la pérdida del día llega al 3 % (`config.MAX_DAILY_LOSS_PCT`, con la cuenta a precio de mercado). ≤ 4 posiciones (`config.MAX_POSITIONS`). Exposición ≤ 100 %, con los perpetuos por su margen. Nominal en perpetuos ≤ 2 × el capital. Nominal ≤ 40 % del capital. |
| Tamaño | Capital × 0,5 % / `paper.stop_loss_frac(lado, stop, 1 + 2 pb)`, con la cuenta a precio de mercado (cuenta ficticia de 100) |
| Instrumento | `apertura.impulse_instrument(d, SEIS2)`: largo al contado y corto en perpetuo |
| Funding | El real de Binance en cada liquidación entre la entrada y la salida (`paper.funding_pay`); la mitad tras la parcial |
| Final del periodo ⚑ | Una operación que acaba «fin de datos» ocupa su moneda pero no cuenta como resultado (como el laboratorio; `medir_selectiva.py` sí la contaba). En el periodo del canario no hay ninguna. |

### 7.2 Comisiones reales ⚑

- **Comisión de la simulación.** `simulate_plan` cobra una sola comisión por lado. Se le pasa la de **tomador** del
  instrumento, con el perfil en uso (`comisiones.tasa(inst, "tomador")`).
- **Ajuste por órdenes límite.** Después se devuelve la diferencia tomador − creador de lo que sale con orden límite (la
  parcial y el objetivo), sobre su nominal negociado. Es exactamente lo que cobra el paper: entrada, stop y cierres, tomador;
  objetivo y parcial, creador.
- **Perfil principal: `okx_eu_con_xperps`.** Contado 0,10 % / 0,08 %; X-Perps 0,05 % / 0,02 %.
- **Sin X-Perps, siempre informado** (8.4). Con `comisiones.elegir("okx_eu_sin_xperps")` dentro de la tarea:
  - Contado 0,35 % / 0,20 %.
  - **Sin cortos**: se quitan.
  - Coste de la regla del 3 %: 0,74 %.
- **Sin efecto en el bot.** El perfil es estado del proceso: la tarea es un proceso aparte.

### 7.3 Lo que devuelve

```python
def medir(m: Mercado, s: Senales, salida: dict, perfil: str = "okx_eu_con_xperps", reglas: bool = True,
          desde_ms: int | None = None, hasta_ms: int | None = None, estres: str | None = None) -> Medida
# Medida.operaciones: [{t, t_fill, ext, coin, d, sl, R, R_f, ret, fund_pct, why, first, partial, locked, mfe, mae,
#                       inst, N, eq, dia, ventana, clave}]   (R_f = con funding: lo que cobra el paper)
# Medida.vetos: Counter (moneda ocupada, tope del día, riesgo del día, plan, stop_lejano, sin_cortos, paper detenido…)
# Medida.cuenta: capital por día de Madrid (para la caída máxima y el % al día compuesto)
```

### 7.4 Estreses (sobre las MISMAS operaciones, como `medir_selectiva.estres`)

| Estrés | Qué cambia |
|---|---|
| `desliz_x2_y_stops` | Deslizamiento × 2; y en los stops, 5 pb más en BTC y ETH y 20 pb en SOL y XRP (libro de USDC de OKX EU, menos profundo) |
| `entrada_1_min_tarde` | Entrada en la vela siguiente |
| `sin_xperps` | Perfil sin X-Perps y sin cortos |
| `objetivo_superado_2pb` | El objetivo y la parcial solo se cobran si el precio los supera en 2 pb, como el laboratorio |
| `contrario` | La misma operación en sentido contrario (`r_opuesto`), para la base emparejada |

### 7.5 Velocidad prevista

Con el libro de `medir_selectiva.py`:
- Las velas en bruto de los 3 años, ≈ 42 s desde los zip; ≈ 1 s desde la caché `.npy`.
- Unas 3.800 operaciones con `simulate_plan`, ≈ 12 s en un núcleo.

Por variante, unos 6 s en la etapa rápida, 15 s en la completa y 60 a 90 s en la de robustez. **La CPU no es el cuello de
botella: lo es el presupuesto estadístico (9).**

### 7.6 Canario (antes de medir nada)

Tiene que pasar al arrancar, al cambiar la firma del código o la de los datos, al cambiar de época y cada domingo:

| Canario | Qué reproduce | Con qué |
|---|---|---|
| **Libro** | `seis2_resumen.json`: **dentro 2.389 operaciones y −0,0569 R (± 0,00006); fuera 1.464 (± 2) y −0,075 R (± 0,0015)** | Las entradas de `ImpulseRun` con el preset SEIS (`news_mode` «etiqueta»), la salida A con stop de 2 % y la comprobación del plan de `SalidaRef` de `medir_selectiva.py`. Aperturas del 28-10-2023 al 20-10-2026, corte el 16-08-2025 y `comisiones.costes_de_backtest(0.10)`. |
| **Comisiones** (desde el 29-10-2026) | 1) La tabla de `bot/comisiones.py`: con X-Perps, contado 0,10 % / 0,08 % y X-Perps 0,05 % / 0,02 %; sin X-Perps, contado 0,35 % / 0,20 % y sin cortos. 2) En las mismas operaciones del canario (hasta 600: todas las de parcial, objetivo y cortos primero), lo que cobra el libro con el perfil real (`okx_eu_con_xperps`, el de todas las medidas: tomador y devolución del creador) es el bruto con el deslizamiento menos la comisión de la fórmula independiente de `controles/costes/bruto_neto.py` (`comision_pct`), al 1e-9 % | El libro con el perfil real y con comisión 0 sobre el mismo camino (`canario.comprobar_comisiones`) |
| **Laboratorio** (solo con el adaptador de la fase 2) | E2 (`ap_mom:15:0,2\|rvol:1,5\|todos\|a_favor\|todas\|todas\|todos\|t:30\|3\|3\|sig`): **278 operaciones fuera y +0,1402 R** | `laboratorio.build_lab("2023-11", …, today=24-10-2026)` |

- **Si falla un canario,** el estado pasa a `canario_fallido`. Se para toda medida: solo sigue la admisión. Se apunta en
  `eventos` y en el diario.
- **Qué comprueba.** `simulate_plan`, `impulse_plan`, `exit_rules` y compañía las está tocando ahora otro flujo: el
  canario es lo que dice si siguen haciendo lo mismo. El del libro va con las comisiones planas del backtest (las de
  `seis2_resumen.json`); el de comisiones cubre lo que cobran de verdad las medidas, que si no nadie comprobaba.
- **Si el código nuevo reproduce las cifras,** los resultados anteriores siguen valiendo.
- **Si no las reproduce,** los resultados se marcan «código anterior». Las candidatas se vuelven a medir; no cuentan en N
  otra vez.

## 8. Validación por etapas

**Cómo se mide en todas las etapas:**
- Libro de 7, perfil con X-Perps y funding. R = `R_f`.
- **Día** = día de Madrid de la entrada.
- **Serie diaria** = suma de R por día natural, con ceros en los días sin operaciones.
- Semillas fijas: hash del id de la variante + 20261029. Los resultados se repiten con los mismos datos.

### 8.1 Etapa 0: admisión

No mira ningún resultado y **no cuenta en N**.

| Código | Comprobación |
|---|---|
| A1 | Formato y campos de 5.3 |
| A2 | Familia registrada y con su versión vigente; su archivo coincide con la firma registrada (11.8) |
| A3 | De 1 a 6 variantes, distintas, con parámetros dentro de su esquema |
| A4 | ≤ 3 filtros activos por variante (los `Param.filtro` que no están en su valor neutro) |
| A5 | Reglas del usuario: riesgo = `config.RISK_PER_TRADE_PCT`, 6 entradas, 3 %, una por moneda, contado y perpetuo, salida del catálogo con `evolucion.cumple_reglas` y plan válido con el stop mínimo (6.5). Si no las cumple: rechazo, salvo `fuera_de_reglas` (5.4). |
| A6 | Datos: cada clave pedida está en la familia y cubre ≥ 50 % del periodo. Si no, solo como exploratoria (8.6). Con `noticias_3a`, la admisión con datos lo mide de verdad: si menos del 50 % de los días del periodo tienen alguna noticia (o no hay archivo compilado), no se mide (21.5). |
| A7 | No duplicada (5.4) |
| A8 | Conformidad con datos reales: prueba del recorte (un corte en cada señal, hasta 60 por tramo, y 20 al azar; 4.4), determinismo, por flanco y velocidad (6.4) |
| A9 | Frecuencia, sin resultados: señales por día natural dentro de muestra ≥ 0,25. Si no, «muy pocas señales». |
| A10 | Lógica: mecanismo, fuentes, predicción y falsación rellenos (5.3) |
| A11 | Cupo del día (11.4). Si no cabe, espera en la cola; no se rechaza. |

### 8.2 Etapa 1: rápida (solo dentro de muestra)

Todas las variantes admitidas, **solo con datos anteriores al corte**: así el fuera de muestra queda para las que
sobreviven. Desde aquí, cada variante **cuenta 1 en N** (una sola vez, aunque luego pase a más etapas).

| Código | Pasa si |
|---|---|
| R1 | ≥ 120 operaciones dentro |
| R2 | R medio dentro > 0 |

### 8.3 Etapa 2: completa (todo el periodo)

Las variantes que pasan la rápida, en todo el periodo:

| Código | Pasa si |
|---|---|
| K1 | ≥ 100 operaciones fuera y ≥ 20 semanas distintas con operaciones fuera |
| K2 | R medio fuera > 0 |
| K3 | R medio > 0 en las dos mitades del fuera de muestra (por calendario) |
| K4 | **Avance trimestral** dentro de la hipótesis (detalle abajo) |
| K5 | **Frecuencia** fuera: ≥ 1,0 operación por día natural, «frecuente»; de 0,25 a 1,0, «selectiva»; < 0,25, no pasa |

**Cómo se calcula K4.** Se recorren los trimestres naturales del fuera de muestra (2025-T3 desde el corte, 2025-T4,
2026-T1…).
- Cada trimestre se elige, entre las variantes vivas de la hipótesis, la de mayor R medio con todo lo anterior
  (ventana creciente, ≥ 50 operaciones previas).
- Se mide lo que dio en ese trimestre.
- Pasa si el R medio de lo elegido es > 0 y son positivos al menos la mitad de los trimestres, redondeando hacia arriba.
- Solo se usan los trimestres del fuera de muestra: la selección de la etapa 1 usó datos anteriores al corte, que son
  pasado para todos ellos.

### 8.4 Etapa 3: robustez

Las variantes que pasan la completa:

| Código | Pasa si |
|---|---|
| B1 | **Base emparejada** fuera, p < 0,05 con 1.000 sorteos (detalle abajo) |
| B2 | Sin sus **5 mayores operaciones** (fuera): R medio > 0 |
| B3 | Sin su **mejor semana** (semana ISO de Madrid con más R, fuera): R medio > 0 |
| B4 | Estrés `desliz_x2_y_stops`: R medio fuera > 0 |
| B5 | Estrés `entrada_1_min_tarde`: R medio fuera > 0 |
| B6 | **Estabilidad:** al menos la mitad de las variantes de la hipótesis dentro de reglas, redondeando hacia arriba, con R medio fuera > 0. Cuenta la idea, no el número. |
| B7 | **Benjamini-Hochberg:** q ≤ 0,10 sobre todas las pruebas acumuladas (9.3) |
| B8 | **Sharpe deflactado** ≥ 0,95 con el N acumulado (9.4) |

**La base emparejada (B1) depende de los sentidos que opera:**
- **En los dos** (el sentido minoritario es ≥ 10 % de las operaciones): las mismas operaciones (horas, monedas y stops)
  con el sentido sorteado y la misma cuota de largos (`base_azar`).
- **En uno solo** (el sorteo del sentido no tendría sentido): el mismo número de operaciones por moneda, en el mismo
  sentido y con la misma salida, a horas al azar del mismo día de la semana y la misma hora (± 1 h).
  - Salen de un fondo de entradas al azar que se simula una vez por época y salida.
  - El stop del fondo es la mediana de los de la estrategia.

**Para la estabilidad (B6)** hace falta el fuera de muestra de todas las variantes. Se calcula también el de las que no
pasaron la rápida (cuentan en N_fuera). Es cuando la candidata ya está elegida, así que no sesga la elección.

**Se informa, pero no decide:**
- Sin X-Perps (contado 0,35 % y sin cortos); objetivo superado en 2 pb; solo largos.
- Por moneda, por ventana, por año y por trimestre.
- MFE y MAE, colas (p1 a p99, mayor y menor), caída máxima en R y de la cuenta, y vetos.
- Comparación emparejada con «6 diarias v2» (`emparejar_seis2`) y meta del 1 % (10.3).

### 8.5 Etapa 4: prueba en avance virtual

La variante pasa a «candidata» y entra en avance desde la medianoche de Madrid siguiente:
- Se mide con el mismo libro sobre los datos que van llegando (caché `vivo`).
- Va en una cuenta virtual propia: una posición por moneda y sus topes.
- Se recalcula cada hora fuera de las pausas. Cada recálculo da todas las operaciones de la variante desde 8 días antes
  de su inicio y **sustituye** las anteriores: una que ya no sale (un hueco de datos que se rellenó) se quita, y si era
  completa se apunta en `eventos` («avance_recalculo»); un resultado más viejo que el último no cambia nada. Las señales
  de cada variante salen con **su** ventana de datos (su inicio menos su calentamiento), así que no cambian porque entre
  o salga otra variante del avance.
- Lo de antes del inicio es calentamiento (posiciones que ya estarían abiertas): no cuenta, como `cuerpo.previa` de la
  vigilancia.
- Una operación es **completa** cuando su entrada es de hace más que su horizonte máximo: A y C, 168 h; H, su
  `max_hold_h`. Así las abiertas no sesgan nada, como en `evolucion.py`.
- Si hay varias variantes de una hipótesis que pasan, entra en avance **una**: la de mayor R medio en todo el periodo.
  Las demás quedan «candidatas hermanas» y no se pueden proponer a la vez.

### 8.6 Exploratorias (sin historia)

Hipótesis con datos que no tienen 3 años: noticias en vivo, ratio largo/corto, libro de órdenes…
- Tras la admisión (sin A9) van **directas a la prueba en avance**, como exploratorias.
- Como mucho **3 a la vez**.
- Tienen un listón más alto (10.2), porque no traen ninguna evidencia de antes.

## 9. Corrección por número de pruebas acumulado

### 9.1 Qué cuenta

| Cifra | Qué es |
|---|---|
| **N_total** | Variantes medidas alguna vez (etapa 1 o más), del laboratorio vivo. Incluye las fuera de reglas y las fallidas. Una vez cada una. Una nueva medida por cambio de época, por estrés o por el canario no suma. |
| **N_fuera** | Variantes cuyo fuera de muestra se ha calculado (etapa 2) |
| **N_previo** | Pruebas hechas antes sobre la misma historia (9.2) |
| Tabla | Cada medida queda en `pruebas`, de **solo añadir**: dos disparadores impiden borrarla o cambiarla, como `evolution_log`. **No hay forma de «limpiar» el N** dentro de la base. |
| Registro fuera de la base | `lab_vivo\n_acumulado.jsonl` (`registro_n.py`), de solo añadir: una línea al arrancar, cada vez que crece el N y en cada consolidación (N total, heredado, acumulado, N efectivo, m de BH, N_dsr e id de la base). Si se borra o se cambia `lab_vivo.db` (o se apunta a otra base en la misma carpeta) y la base tiene **menos** pruebas que el registro, el corredor **no mide nada** (estado `error`, sigue vivo). Solo lo desbloquea el usuario con `heredar-n --nota "…"`: lo que falta pasa a `pruebas_previas` como `lab_vivo_anterior:<fecha>` (p = 1 en BH y su N efectivo en N_dsr) y queda en `eventos` y en el registro. Si se borra la carpeta entera, se pierde también: el N de la primera tanda queda en 20.7. |

### 9.2 Pruebas previas (tabla `pruebas_previas`)

La siembra la hace el implementador contando estos archivos (cifras del 29-10-2026):

| Estudio | Pruebas | Archivo | Mismo fuera de muestra |
|---|---|---|---|
| Backtest general | 690 | `BACKTEST_DIR/resultados.json` | en parte (otro diseño, 10 años) |
| Laboratorio de 2.000 | 2.000 (381 efectivas) | `laboratorio.json` (con `p_day` de cada una en `all[].oos`) | sí |
| ¿6 diarias? | 432 | `controles/seis_diarias.json` | sí (corte 13-08-2025) |
| Salidas («estirar el chicle») | 378 | `controles/salidas_motor.json` (`todas`) | sí |
| Salidas 1:1 | 66 | `controles/salidas_1a1.json` | sí |
| Objetivos pequeños | 196 | `controles/objetivos_pequenos.json` | sí |
| Selectivas (Murphy, Fibonacci, Ruptura) | 23 | `controles/selectiva/medir_selectiva.json` (`resumen`) | sí |
| Técnicas del curso | 52 | `controles/curso/medir_curso.json` (`tabla`) | sí |
| Noticia confirmada | 80 | `controles/noticia_confirmacion.json` | en parte |
| **Total** | **≈ 3.917** | | |

### 9.3 Benjamini-Hochberg (B7)

`laboratorio.bh_qvalues` sobre los p de todas las pruebas:

| Pruebas | Qué p llevan |
|---|---|
| Variantes del laboratorio vivo con fuera de muestra y ≥ 100 operaciones fuera | p de una cola de la **t HAC** (`laboratorio.hac_t`, 7 días de retardo) de su serie diaria fuera, con `laboratorio.t_sf(t, días − 1)` |
| Las de menos operaciones | p = 1 (cuentan en el total m) |
| Previas: los 2.000 | Su `p_day` |
| Las demás previas | p = 1, una por prueba. Es lo prudente: suben m y endurecen el umbral. |

Se recalcula en cada consolidación (11.9), así que el estado de candidata **se puede perder** (9.7).

### 9.4 Sharpe deflactado (B8)

`laboratorio.deflated_sharpe(sr, T, asimetría, curtosis, None, N_dsr)`:
- `sr`, `T`, `asimetría` y `curtosis` son los de la serie diaria fuera de esa variante (`laboratorio.moments`).
- `N_dsr` = N efectivo del laboratorio vivo + 381 (el laboratorio de 2.000) + 1.917 (el resto de previas, contadas
  enteras por prudencia) + el N efectivo heredado de una base anterior (9.1, `registro_n.py`).
- **N efectivo** = el mayor de:
  - `laboratorio.effective_trials` (Li y Ji, 2005) sobre las series diarias fuera de todas las variantes con etapa 2,
    alineadas en su tramo común;
  - la suma de Li y Ji **por hipótesis** (≤ 6 series cada una) más 1 por cada hipótesis medida que no llegó al fuera de
    muestra. Entre hipótesis se suman como independientes: lo prudente.
- **Por qué el suelo.** Li y Ji sobre miles de series de ≈ 411 días **satura**: la matriz de correlación tiene rango ≤
  días y la fórmula no pasa de ≈ 2 × días. Con series independientes, donde el N verdadero es k, da 478 con k = 800, 608
  con 1.600 y 619 con 3.200 (revisión del 29-10-2026). Sin el suelo, el listón de B8 se quedaría en z ≈ 3,55 justo
  cuando se prueban muchas al día; con el N real, 3,71 o más. El suelo crece con cada hipótesis (como el m de BH).
- **Aviso.** Cuando Li y Ji llega a la mitad de los días del fuera de muestra, la consolidación lo dice (`aviso_n`, en
  `estado.json` y en `para_el_generador.json`).
- **Fase 2:** se sacan una vez las series diarias de los 2.000 (volviendo a pasar `laboratorio.evaluate_all`, ≈ 4 min)
  para calcular un N efectivo conjunto. Es más preciso que sumar 381.

### 9.5 En avance: Holm acumulado

La prueba de V3 (10.2) es la de `evolucion.bootstrap_mejora(ops, None, 0)`:
- Usa el p más prudente del remuestreo por bloques de 7 días y de la t robusta por bloques.
- Corrige con **Holm entre todas las variantes que han entrado alguna vez en avance**. k crece y no se resetea.
- Las exploratorias tienen su propio Holm, entre exploratorias.

### 9.6 Cuenta honesta del laboratorio (solo se informa)

Cada trimestre del fuera de muestra se eligen las 10 mejores variantes con todo lo anterior: R medio con ≥ 100
operaciones previas, como `laboratorio.walk_forward_q`. Después se mide qué dieron en el trimestre.
- **Por qué importa.** Si las «mejores» de un trimestre no repiten en el siguiente, el laboratorio está encontrando
  ruido. Es la cifra que en el laboratorio de 2.000 dio −0,118 R (−61 % de la cuenta).
- **Dónde sale.** En `estado.json` y en el diario. No es un criterio.

### 9.7 Revocación

En cada consolidación (cada noche y al cambiar de época) se recalculan B7 y B8 con el N de ese momento.
- **Si una candidata deja de pasar,** queda «revocada». Su prueba en avance sigue, porque los datos salen gratis, pero no
  se puede proponer mientras no vuelva a pasar. Si ya estaba propuesta, su propuesta pasa a «retirada» con el motivo y
  ya no se puede aceptar (13.6).
- **Cada cambio se apunta** en `eventos` y en el diario.

## 10. Candidata y lista para proponer (criterios v1)

**Criterios v1, fijados el 29-10-2026 antes de medir nada.**
- Están en `bot/lab_vivo/criterios.py`, con `VERSION = 1`, la fecha y un sha que va en cada resultado.
- Solo se cambian con una versión nueva, con el visto bueno del usuario y apuntada en `eventos`.
- **Nunca se aplican hacia atrás para aprobar algo.**

### 10.1 Candidata

Una variante **dentro de reglas** que pasa A1–A11, R1–R2, K1–K5 y B1–B8. Entra en la prueba en avance (8.5).

### 10.2 Lista para proponer al usuario

Una candidata en avance pasa a «lista» con todo esto:

| Código | Condición |
|---|---|
| V1 | Sigue siendo candidata en la última consolidación: B7 y B8 con el N de hoy |
| V2 | ≥ **40** operaciones completas en avance, en ≥ 15 días distintos y con ≥ **28** días naturales desde el inicio. Es la vara del usuario: R63 de `docs/requisitos-usuario.md`, al menos 4 semanas y más de 40 operaciones. |
| V3 | R medio en avance > 0 con p < 0,05 corregida por Holm acumulado (9.5) |
| V4 | Caída máxima de la cuenta virtual en avance < **6 %** (R63), con las operaciones cerradas |
| V5 | Frecuencia en avance dentro de su clase: «frecuente», ≥ 1 al día; «selectiva», ≥ 0,25 |

**Exploratorias:** ≥ 100 operaciones completas y ≥ 45 días; p < 0,01 con Holm entre exploratorias; V4 y V5 igual.

**Se descarta en avance** («descartada_avance») en cualquiera de estos casos, también mientras espera la decisión del
usuario (entonces su propuesta pasa a «retirada»):
- Con ≥ 40 operaciones completas, su R contraído (`evolucion.contraido`) es < 0 y el p de que pierde es < 0,05.
- Con ≥ 200 operaciones completas, aún no cumple V3: «no se distingue del azar».

### 10.3 La meta del 1 % (informada, nunca filtro)

Para cada variante con etapa 2 o más, y en avance:

| Campo | Qué es |
|---|---|
| `R_dia` | Suma de R / días naturales del tramo |
| `pct_dia` | `R_dia` × 0,5 % |
| `pct_dia_compuesto` | (capital final / inicial)^(1 / días) − 1 de la cuenta del libro |
| `falta_R_dia` | 2 − `R_dia` |
| `falta_pct_dia` | 1 − `pct_dia` |
| `texto` | Por ejemplo: «Le faltan 1,42 R al día (0,71 % de la cuenta con 0,5 % de riesgo). El riesgo no se sube.» |

Para las familias de apertura también por jornada con aperturas. La web lo enseña siempre al lado de la cifra.

### 10.4 Qué lleva una propuesta

Todo lo de 13.6:
- La regla en español llano (`Familia.describir`).
- Las cifras dentro, fuera y en avance, la robustez, la meta y el resultado sin X-Perps.
- **Las reglas:** si su salida cierra por tiempo antes de la ganancia mínima (todas las del catálogo, 6.5), la
  excepción con lo medido fuera («cerraron así 333 de 898: 320 entre −1R y +2R y 229 ganando menos de 2R»). Para
  aceptarla, el usuario acepta también esa excepción.
- **Cómo se operaría:**
  - **`apertura_impulso`:** su variante se traduce a parámetros del motor (`hipotesis.a_params_motor`, como
    `evolucion.lab_a_params`). Si `evolucion.cumple_reglas` los acepta, el usuario puede aplicarla desde Evolución
    (`estrategia_activa.json`, el mecanismo de siempre).
  - **Cualquier otra familia:** hace falta un `modelo` nuevo en el motor, como «ruptura3» del 29-10-2026, que programa el
    coordinador. Hasta entonces sigue en avance.

En los dos casos lo decide el usuario.

## 11. El corredor 24/7

### 11.1 Bucle del supervisor (cada 5 s)

1. Lee los mandos: `parar`, `pausa_manual`, las decisiones del usuario (`decisiones\`) y las órdenes que le dejan las
   de `__main__` (`heredar_n.json` y `consolidar.ahora`: con el corredor en marcha, solo escribe él en la base).
2. Calcula si toca pausa (11.5) y mira los recursos (11.6).
3. Ingiere los resultados terminados (`trabajos\*.resultado.json`) en `lab_vivo.db`, en una transacción por resultado.
   Si crece el N, lo apunta en `n_acumulado.jsonl` (9.1).
4. Admite las hipótesis nuevas de `cola\entrada\` (A1–A7 y A10 aquí; A8 y A9 en un trabajo de admisión).
5. Lanza trabajos en los huecos libres, por prioridad (11.3).
6. Escribe `estado.json` cada 30 s o al cambiar algo (atómico: `.tmp` y `os.replace`). Las tarjetas, el embudo, la meta
   y `mejores.json` solo se recalculan si ha cambiado algo (o cada 10 min) y **nunca en una pausa**: en la pausa,
   `estado.json` lleva el latido con las últimas tarjetas (`tarjetas_ts` dice de cuándo son).
7. Tareas con hora:

| Tarea | Cuándo (Madrid) |
|---|---|
| Datos vivos y avance | Cada hora a los :20 |
| Consolidación | 04:30 (fuera de las pausas en invierno y en verano) |
| Cierre del diario | 00:05 |
| Cambio de época | El día 1 a las 05:00 |
| Canario | Domingos a las 05:30 |

Si una tarea cae en una pausa, se hace al acabar.

### 11.2 Trabajos

| Tipo | Objetivo | Duración | Tope (`max_s`) |
|---|---|---|---|
| `datos` | Caché de la época (4.3) | ≈ 5 min | 30 min |
| `canario` | 7.6 | ≈ 3 min | 30 min |
| `admision` | A8–A9 de una hipótesis | ≤ 1 min | 10 min |
| `rapida` | Todas las variantes de una hipótesis, dentro de muestra | ≈ 1 min | 15 min |
| `completa` | Sus supervivientes, en todo el periodo | ≈ 2 min | 30 min |
| `robustez` | Una variante | ≈ 2 min | 30 min |
| `avance` | Todas las variantes en avance | ≤ 2 min | 15 min |
| `consolidacion` | 9.3–9.7, salidas y diario | ≤ 5 min | 30 min |

- **Encargo:** cada trabajo es `trabajos\<id>.trabajo.json` (tipo, objetivo, época, firmas, carpeta de la caché, versión
  de los criterios y semilla).
- **Ida y vuelta:** el subproceso escribe `<id>.resultado.json` (y `<id>.npz` con las series y las operaciones),
  `<id>.punto.json` tras cada paso (una variante) y `<id>.log`.
- **Id determinista:** hash de objetivo + tipo + firma de datos + firma de código. Ingerir dos veces el mismo resultado
  no hace nada.
- **Si se pasa de `max_s`,** se mata y falla: «demasiado lento».

### 11.3 Prioridades y equidad

1. `datos` y `canario` (bloquean lo demás).
2. `admision`.
3. `avance`.
4. `robustez`.
5. `completa`.
6. `rapida`.
7. `consolidacion` (cuando le toca la hora pasa por delante).

Dentro de un tipo:
- Primero la `prioridad` de la hipótesis (1 a 5).
- Luego, **turno entre familias**: ninguna acapara la cola.
- Luego, por orden de llegada.

### 11.4 Cupo

| Cupo | Tope | Qué cuenta |
|---|---|---|
| Del día (Madrid) | **24 hipótesis** que empiezan la etapa rápida, ≤ 144 variantes (`BOT_LAB_VIVO_CUPO_DIA`) | Lo que no cabe espera al día siguiente, por orden. |
| Por familia | ≤ 8 hipótesis al día | |
| Por pasada del generador | ≤ 6 hipótesis | Lo comprueba la admisión con `autor` y la hora. |

El cupo protege el N (2.2), no la CPU. Lo puede cambiar el usuario.

### 11.5 Pausas en las aperturas

- **Agenda.** `agenda.py` calcula con `apertura.openings_on(día)` las ventanas de `apertura.LAB_WINDOWS` de hoy y de los
  7 días siguientes, y la guarda en `agenda.json`.
- **Pausa** = desde 5 min antes hasta 30 min después de cada apertura. Se juntan los tramos que se solapan.

| Apertura | Pausa en invierno | Pausa en verano |
|---|---|---|
| Tokio y Seúl | 00:55–01:30 | 01:55–02:30 |
| Shanghái y Hong Kong | 02:25–03:00 | 03:25–04:00 |
| Europa | 08:55–09:30 | 08:55–09:30 |
| São Paulo | 13:55–14:30 | 14:55–15:30 (se junta con Nueva York: 14:55–16:00) |
| Nueva York | 15:25–16:00 (14:25–15:00 en las semanas de desfase del cambio de hora) | 15:25–16:00 |

  Son unas 3 h por día laborable. Los fines de semana y los días con todas las bolsas de una ventana en festivo no hay
  pausa.
- **Cómo se para:**
  1. A falta de < 3 min para una pausa no se empieza ningún trabajo.
  2. En T − 5 min el corredor crea `pausa.ahora`. Las tareas lo miran entre pasos (≤ 30 s), guardan su punto y salen con
     el código 75 («pausado»).
  3. En T − 3 min, la que quede se mata (`TerminateProcess`) y se vuelve a poner en cola desde su último punto, sin gastar
     intento.
  4. Desde T − 3 min hasta T + 30 min, CPU del laboratorio ≈ 0. El supervisor solo escribe `estado.json`.
- **Si no se puede importar `apertura`** (p. ej., a medio editar por otro flujo), vale la `agenda.json` anterior, que dura
  7 días.
- **Sin agenda, modo prudente:** en los días laborables, pausa de 00:50 a 04:05, de 08:50 a 09:35 y de 13:50 a 16:05 de
  Madrid. Cubre invierno y verano. Se avisa en `estado.json`.
- **Pausa manual:** con el archivo `pausa_manual` no se empieza nada.

### 11.6 Recursos (Windows, solo con la biblioteca estándar y `ctypes`; sin psutil)

**Prioridad.** BELOW_NORMAL (`SetPriorityClass`). La heredan las tareas.

**Afinidad.** 3 de los 6 núcleos (`SetProcessAffinityMask`; por defecto las CPU 3, 4 y 5). El bot y la IA local tienen
siempre libres los demás.

**Job Object** (`CreateJobObjectW` + `SetInformationJobObject`, información extendida). El corredor se mete en él
(`AssignProcessToJobObject`) y sus hijos entran solos. Límites:

| Límite | Valor |
|---|---|
| `JOB_OBJECT_LIMIT_JOB_MEMORY` | Memoria total del laboratorio, 2.500 MB por defecto (`BOT_LAB_VIVO_MEMORIA_MB`). Si un cálculo la pasa, numpy da `MemoryError`: el trabajo falla con «memoria» y se reintenta con un solo proceso. |
| `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` | Si el corredor muere, mueren sus tareas: nunca quedan cálculos huérfanos en una pausa. |
| `JOB_OBJECT_LIMIT_PRIORITY_CLASS` y `JOB_OBJECT_LIMIT_AFFINITY` | Las mismas que el corredor |

**Procesos de cálculo.** Como mucho 2 a la vez por defecto (`BOT_LAB_VIVO_PROCESOS`; 3 como máximo).
- Siempre se trabaja con 1. El segundo (y el tercero) solo se lanzan si la memoria física libre
  (`GlobalMemoryStatusEx`) es ≥ 4 GB.
- El 29-10-2026 a las 10:57 el PC tenía 15,9 GB y solo 2,0 GB libres, con el bot y la IA local en marcha: hoy
  trabajaría con 1.

**Guarda de memoria:**

| Memoria libre | Qué hace |
|---|---|
| < 1.500 MB (`BOT_LAB_VIVO_MIN_LIBRE_MB`) | No se empieza nada |
| < 800 MB | Se mata la tarea más reciente y vuelve a la cola: el bot y la IA local van primero |

**Freno por CPU.** Si la CPU de todo el PC (`GetSystemTimes`) pasa del 90 % durante 60 s, no se empieza nada.

**Hilos de BLAS.** Las tareas llevan `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `MKL_NUM_THREADS` y
`NUMEXPR_NUM_THREADS` = 1: `eigvalsh` y compañía no abren 6 hilos cada una.

**Temporales.** `TMP` y `TEMP` = `lab_vivo\tmp` (en M:, nunca en C:).

**GPU.** No se usa: la RTX es de la IA local del bot.

**En Linux** (Claude en la nube, solo para las pruebas): `os.nice(10)` y sin límites.

### 11.7 Reanudación tras una caída

| Paso | Qué hace |
|---|---|
| Arranque | El corredor toma `corredor.lock` (PID, hora, **hora de creación del proceso** y orden). Si hay otro vivo, sale. «Vivo» = el PID existe (`OpenProcess` y `GetExitCodeProcess`) **y** se creó cuando dice el lock (`GetProcessTimes`, ± 2 s; un lock antiguo sin esa hora: creado antes del lock). Un lock viejo cuyo PID ha reutilizado Windows (tras un reinicio, un apagón o un `taskkill`) es de otro proceso: se reemplaza y no se toca. Después comprueba el N con `n_acumulado.jsonl` (9.1). |
| Ingesta | Ingiere los `*.resultado.json` pendientes y borra los `*.tmp` a medias. |
| Trabajos `en_curso` | Pasan a `pendiente`, con intentos + 1, desde su `punto_control`. Sus procesos murieron con el Job Object. Los pausados no gastan intentos. |
| Tres fallos | El trabajo queda `fallido`, con el error y su `.log`. La hipótesis, «error», sin descartarla; se ve en la web. |
| Una tarea que se cuelga | Su `max_s` la mata. |
| Un corredor colgado | Lo mata y relanza el vigilante (apartado 16). |
| Durabilidad | SQLite en WAL y escrituras atómicas de los JSON: un apagón no deja nada a medias. |
| Código del bot que no se importa | Un `SyntaxError` en un módulo que esté editando otro flujo no tumba nada: la tarea falla con «código del bot no importable» y el corredor espera 10 min sin gastar intentos. |

### 11.8 Familias y código

**Registro de familias.** Las tareas descubren las familias escaneando `bot/lab_vivo/familias/` **en cada subproceso**.
- **Una familia nueva no necesita reiniciar nada.**
- **Una que falla solo tumba sus propias tareas.**

**Firma de una familia.** La tabla `familias` guarda (nombre, versión, sha del archivo).
- **Archivo cambiado sin subir la versión:** la familia no se usa. Estado «familia cambiada sin subir versión», porque sus
  resultados no serían comparables.
- **Error grave encontrado después** (p. ej., que mira al futuro): se invalida esa versión. Sus pruebas siguen contando
  en N.

**Firma del código.** Sha de `bot/lab_vivo/**` y de los módulos del bot que usa: `apertura`, `paper`, `comisiones`,
`config`, `laboratorio`, `backtest_aperturas`, `historia_extra`, `evolucion`, `vigilancia`, `regions`, `backtest` y
`history`. Va en cada prueba y, si cambia, se vuelve a pasar el canario.

**Arranque.** El corredor y las tareas exigen `config.USER_RULES` activo y `RISK_PER_TRADE_PCT` = 0,5. Si no, no miden:
estado `error`, con el motivo.

### 11.9 Consolidación (cada noche)

1. BH, N efectivo, Sharpe deflactado y Holm del avance (9.3–9.5); revocaciones (9.7).
2. Cuenta honesta del laboratorio (9.6).
3. **Lecciones por familia**, calculadas, sin IA: cuántas variantes pasan cada etapa, el motivo de fallo más común y los
   rangos de parámetros probados.
4. Escribe:
   - `mejores.json`, `hipotesis.json`, `hipotesis\<id>.json` y `cola.json`.
   - `para_el_generador.json` y `ya_probado.json`.
   - Las propuestas nuevas (`propuestas\`).
   - El resumen del día en `diario\`.
5. Limpia los trabajos de hace más de 7 días (deja sus registros 30 días).

### 11.10 Órdenes y ajustes

`python -X utf8 -m bot.lab_vivo <orden>`:

| Orden | Qué hace |
|---|---|
| `correr [--procesos N] [--sin-pausas] [--una-vez]` | El supervisor (`--sin-pausas` y `--una-vez` solo para pruebas) |
| `correr --una-pasada [--max-horas H]` | Todo lo que haya en la cola, una vez, con las mismas pausas, prioridad, afinidad, Job Object y guardas. Sin avance por horas ni consolidación de las 04:30: al acabar consolida (fuera de las pausas) y se para. Lo que espera al cupo de otro día no la frena. Para la primera tanda (20.7). |
| `tarea <id>` | Un trabajo (lo lanza el corredor) |
| `validar <archivo.json>` | La admisión completa sin medir resultados: lo que usa el generador antes de enviar (15.3). En una pausa de las aperturas no calcula A8 ni A9 (los hará el corredor) y, si empieza una a medias, se para. |
| `estado` | Resumen en texto del `estado.json` |
| `canario` | Los dos canarios ahora (7.6) |
| `preparar-datos` | La caché de la época |
| `consolidar` | La consolidación ahora. Con el corredor en marcha deja `consolidar.ahora` y la hace él (es el único que escribe en la base), fuera de las pausas. |
| `heredar-n --nota "…"` | La orden del usuario para una base con menos pruebas que `n_acumulado.jsonl` (9.1). Con el corredor en marcha deja `heredar_n.json` y la aplica él. |

**Las órdenes que calculan** (`validar`, `canario`, `preparar-datos` y `consolidar`) van con prioridad por debajo de lo
normal y en los núcleos del laboratorio, como el corredor, y no empiezan en una pausa de las aperturas ni a menos de 3
min de una: `validar` deja A8 y A9 para el corredor; las demás se niegan (código 75) y hay que repetirlas después.

Ajustes (variables `BOT_LAB_VIVO_*`, leídas en `bot/lab_vivo/ajustes.py`; `config.py` no se toca):

| Variable | Por defecto |
|---|---|
| `BOT_LAB_VIVO` | `1` (`0`: el corredor no arranca) |
| `BOT_LAB_VIVO_DIR` | `DATA_DIR\lab_vivo` |
| `BOT_LAB_VIVO_PROCESOS` | `2` (como mucho `3`; desde el segundo, solo con ≥ 4 GB libres) |
| `BOT_LAB_VIVO_CPUS` | `3,4,5` |
| `BOT_LAB_VIVO_MEMORIA_MB` | `2500` |
| `BOT_LAB_VIVO_MIN_LIBRE_MB` | `1500` |
| `BOT_LAB_VIVO_CUPO_DIA` | `24` |
| `BOT_LAB_VIVO_CUPO_PASADA` | `6` |
| `BOT_LAB_VIVO_API` | `http://127.0.0.1:8765` (fase 2: noticias en vivo) |

Las pausas (5 min antes y 30 después) son constantes: las pidió el usuario.

**Registro:**
- `registro\corredor.log` (`RotatingFileHandler`, 5 MB × 5, UTF-8, en español).
- Un `trabajos\<id>.log` por tarea.
- Una traza que se repite sale como mucho una vez cada 5 min, como la vigilancia.

## 12. lab_vivo.db

**Base:** `M:\BotFundamental-datos\lab_vivo\lab_vivo.db`.
- Modo WAL y `busy_timeout` de 30 s.
- **Solo escribe el corredor.** Las tareas solo leen, en modo `ro`.
- El servidor del bot no la abre: lee los JSON (14.1).

```sql
CREATE TABLE IF NOT EXISTS hipotesis(id TEXT PRIMARY KEY, recibida INTEGER NOT NULL, autor TEXT, familia TEXT NOT NULL,
  familia_version INTEGER, titulo TEXT, firma TEXT NOT NULL, estado TEXT NOT NULL, prioridad INTEGER, exploratoria INTEGER,
  fuera_de_reglas INTEGER, cuerpo TEXT NOT NULL, admision TEXT, actualizada INTEGER);
CREATE TABLE IF NOT EXISTS variantes(id TEXT PRIMARY KEY, hipotesis TEXT NOT NULL, vid TEXT NOT NULL,
  firma TEXT NOT NULL UNIQUE, params TEXT NOT NULL, salida TEXT NOT NULL, cumple_reglas INTEGER NOT NULL,
  clase TEXT NOT NULL, etapa TEXT, motivos TEXT, frecuencia TEXT, avance_desde INTEGER, actualizada INTEGER);
CREATE TABLE IF NOT EXISTS pruebas(id INTEGER PRIMARY KEY AUTOINCREMENT, variante TEXT NOT NULL, etapa TEXT NOT NULL,
  ts INTEGER NOT NULL, epoca TEXT, firma_datos TEXT, firma_codigo TEXT, criterios INTEGER, perfil TEXT,
  cuenta_en_n INTEGER NOT NULL, n_dentro INTEGER, n_fuera INTEGER, R_dentro REAL, R_fuera REAL, p_fuera REAL,
  sr_fuera REAL, dias_fuera INTEGER, asimetria REAL, curtosis REAL, pasa INTEGER, motivos TEXT, metricas TEXT);
CREATE TABLE IF NOT EXISTS series(prueba INTEGER PRIMARY KEY, desde_dia TEXT NOT NULL, dias INTEGER NOT NULL,
  r_dia BLOB NOT NULL);                                   -- float32 comprimido con zlib: R por día natural
CREATE TABLE IF NOT EXISTS operaciones(variante TEXT NOT NULL, tramo TEXT NOT NULL, k INTEGER NOT NULL,
  cuerpo TEXT NOT NULL, PRIMARY KEY(variante, tramo, k));   -- solo de candidatas y listas (dentro, fuera)
CREATE TABLE IF NOT EXISTS avance(id TEXT PRIMARY KEY, variante TEXT NOT NULL, moneda TEXT, lado TEXT,
  t_senal INTEGER, t_entrada INTEGER, t_salida INTEGER, R REAL, R_f REAL, pct REAL, motivo TEXT,
  completa INTEGER, previa INTEGER, cuerpo TEXT);         -- id = variante:moneda:t_entrada (un reinicio no duplica);
                                                          -- cada recálculo sustituye TODAS las filas de la variante
CREATE TABLE IF NOT EXISTS trabajos(id TEXT PRIMARY KEY, tipo TEXT NOT NULL, objetivo TEXT, prioridad INTEGER NOT NULL,
  estado TEXT NOT NULL, intentos INTEGER NOT NULL DEFAULT 0, creado INTEGER, empezado INTEGER, terminado INTEGER,
  pid INTEGER, punto_control TEXT, segundos REAL, error TEXT);
CREATE TABLE IF NOT EXISTS consolidaciones(ts INTEGER PRIMARY KEY, n_total INTEGER, n_fuera INTEGER, n_efectivo REAL,
  n_previo INTEGER, n_dsr REAL, bh_m INTEGER, cuerpo TEXT);
CREATE TABLE IF NOT EXISTS pruebas_previas(fuente TEXT PRIMARY KEY, n INTEGER NOT NULL, n_efectivo REAL,
  mismo_fuera INTEGER, p_conocidos INTEGER, archivo TEXT, nota TEXT);
CREATE TABLE IF NOT EXISTS previas_p(fuente TEXT NOT NULL, k INTEGER NOT NULL, p REAL, PRIMARY KEY(fuente, k));
CREATE TABLE IF NOT EXISTS familias(nombre TEXT NOT NULL, version INTEGER NOT NULL, sha TEXT NOT NULL, alta INTEGER,
  PRIMARY KEY(nombre, version));
CREATE TABLE IF NOT EXISTS noticias_vivas(id TEXT PRIMARY KEY, t INTEGER NOT NULL, cuerpo TEXT);   -- fase 2
CREATE TABLE IF NOT EXISTS eventos(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, tipo TEXT NOT NULL,
  sujeto TEXT, texto TEXT NOT NULL, cuerpo TEXT);
CREATE TABLE IF NOT EXISTS estado_kv(clave TEXT PRIMARY KEY, valor TEXT);
CREATE TRIGGER IF NOT EXISTS pruebas_sin_borrar BEFORE DELETE ON pruebas
  BEGIN SELECT RAISE(ABORT, 'pruebas: no se borran: son el número de pruebas acumulado'); END;
CREATE TRIGGER IF NOT EXISTS pruebas_sin_cambios BEFORE UPDATE ON pruebas
  BEGIN SELECT RAISE(ABORT, 'pruebas: no se modifican'); END;
-- los mismos dos disparadores en series y en eventos
```

**Valores:**

| Columna | Valores |
|---|---|
| `variantes.clase` | `pendiente`, `duplicada`, `rechazada`, `fuera_de_reglas`, `descartada_rapida`, `descartada_completa`, `descartada_robustez`, `candidata`, `candidata_hermana`, `revocada`, `en_avance`, `descartada_avance`, `lista`, `propuesta`, `aceptada`, `descartada_usuario`, `invalidada`, `error` |
| `pruebas.etapa` | `rapida`, `completa`, `robustez`, `avance`, `estres`, `canario`, `referencia` |
| `pruebas.cuenta_en_n` | 1 solo en la primera medida de una variante (la rápida, o la de avance en una exploratoria) |

## 13. Salidas para la web y para el generador

**Carpeta:** `M:\BotFundamental-datos\lab_vivo\`. Todas se escriben de forma atómica (`.tmp` y `os.replace`).
- Los números del JSON van como números y los textos, en español con coma decimal.
- Cada archivo lleva `formato: 1` y `ts`.

```
lab_vivo\
  lab_vivo.db              corredor.lock          agenda.json            n_acumulado.jsonl (9.1, solo se añade)
  estado.json              mejores.json           cola.json
  hipotesis.json           hipotesis\<id>.json    ya_probado.json        para_el_generador.json
  para_el_generador_estado.json                   (lo único del estado que lee el generador: 13.5)
  diario\AAAA-MM-DD.json   generador\AAAA-MM-DD-HHMM.json
  cola\entrada\  cola\aceptadas\  cola\rechazadas\
  propuestas\<id>.json     decisiones\<id>.json
  trabajos\                cache\<época>-<firma>\  cache\vivo\     registro\   tmp\
  parar  ·  pausa_manual  ·  pausa.ahora  ·  consolidar.ahora  ·  heredar_n.json   (control: existen o no)
```

### 13.1 `estado.json` (cada 30 s o al cambiar)

```json
{"formato": 1, "ts": 0, "latido": 0, "pid": 1234, "arranque": 0, "version": "lab_vivo 1",
 "estado": "activo|pausa|en_espera|sin_datos|canario_fallido|error|arrancando|parado",
 "motivo": "Pausa por la apertura de Europa (09:00) hasta las 09:30",
 "aviso": "Laboratorio de investigación: nada de esto opera. Lo que aguanta pasa a una prueba en avance VIRTUAL y a la cartera paper solo si tú lo decides.",
 "proxima_pausa": {"ventana": "europa", "nombre": "Europa", "desde": 0, "hasta": 0},
 "recursos": {"procesos": 1, "procesos_max": 2, "cpus": [3, 4, 5], "prioridad": "por debajo de lo normal",
              "memoria_libre_mb": 2048, "memoria_limite_mb": 2500, "memoria_usada_mb": 910, "limitado_por": "memoria|cpu|pausa|null"},
 "datos": {"epoca": "2026-11", "periodo": {"desde": "2023-11-01", "hasta": "2026-10-31", "corte": "2025-08-16"},
           "firma": "ab12cd34", "datos_hasta": 0, "vivo_hasta": 0, "atrasados": false},
 "codigo": {"firma": "…", "canario": {"estado": "ok|fallido|pendiente", "ts": 0, "comisiones": true,
            "libro": {"dentro_n": 2389, "dentro_R": -0.0569, "fuera_n": 1464, "fuera_R": -0.075, "reproduce": true}}},
 "criterios": {"version": 1, "fecha": "2026-10-29", "sha": "…"},
 "contadores": {"hipotesis": {"total": 0, "hoy": 0, "en_cola": 0, "cupo_hoy": 24},
                "n": {"total": 0, "fuera": 0, "efectivo": 0.0, "previo": 3917, "dsr": 0.0},
                "descartes_por_motivo": {"R2": 0, "K2": 0, "B1": 0}},
 "embudo": [{"etapa": "recibidas", "n": 0}, {"etapa": "admitidas", "n": 0}, {"etapa": "pasan_rapida", "n": 0},
            {"etapa": "pasan_completa", "n": 0}, {"etapa": "candidatas", "n": 0}, {"etapa": "en_avance", "n": 0},
            {"etapa": "listas", "n": 0}],
 "meta": {"objetivo_pct_dia": 1.0, "riesgo_pct": 0.5, "objetivo_R_dia": 2.0, "mejor": {"variante": "…", "R_dia": 0.0,
          "pct_dia": 0.0, "tramo": "fuera|avance"}, "falta_R_dia": 2.0, "falta_pct_dia": 1.0, "texto": "…"},
 "en_curso": [{"trabajo": "…", "tipo": "rapida", "hipotesis": "…", "paso": "V3", "desde": 0, "progreso": 0.5}],
 "cola": {"pendientes": 0, "por_tipo": {"rapida": 0}, "siguientes": [{"hipotesis": "…", "tipo": "rapida", "prioridad": 3}]},
 "mejores": [], "en_avance": [], "listas": [],
 "consolidacion": {"ts": 0, "umbral_dsr_z": 3.6, "bh_m": 0, "n_dsr": 2400.0, "aviso_n": null,
                   "cuenta_honesta": {"trimestres": [], "R_medio": null}},
 "tarjetas_ts": 0,
 "diario_hoy": {"dia": "2026-10-29", "lineas": ["…"]},
 "errores": [{"ts": 0, "texto": "…"}]}
```

Las listas `mejores`, `en_avance` y `listas` llevan como mucho 10 **tarjetas** (13.3). `tarjetas_ts` es cuándo se
calcularon: en las pausas de las aperturas no se recalculan (11.1).

### 13.2 `diario\AAAA-MM-DD.json` (se va rellenando; se cierra a las 00:05)

```json
{"formato": 1, "dia": "2026-10-29", "ts": 0,
 "hipotesis": {"recibidas": 12, "admitidas": 10, "rechazadas": [{"id": "…", "motivos": ["A7: ya probada (…)"]}]},
 "variantes": {"rapida": 48, "completa": 20, "robustez": 4, "n_fin_del_dia": 1234},
 "candidatas_nuevas": [], "revocadas": [], "avance": [{"variante": "…", "nuevas": 3, "completas": 17, "R_medio": 0.21}],
 "listas_nuevas": [], "pausas": [{"ventana": "europa", "desde": 0, "hasta": 0}], "cpu_horas": 9.5, "errores": 0,
 "lecciones": ["ruptura_canal: 6 variantes, 4 pasan la rápida y 0 la completa (K2: negativas fuera de muestra)"],
 "generador": ["2026-10-29-1215: 6 hipótesis (2 de familia nueva lider_btc)"],
 "texto": ["Hoy: 10 hipótesis admitidas, 48 variantes medidas; ninguna candidata nueva.", "…"]}
```

### 13.3 Tarjeta de una variante (en `mejores`, `en_avance`, `listas` y `hipotesis\<id>.json`)

```json
{"id": "h-…:V2", "hipotesis": "h-…", "titulo": "…", "familia": "ruptura_canal", "regla": "texto llano (describir)",
 "clase": "candidata", "cumple_reglas": true, "fuera_de_reglas": false, "frecuencia": "frecuente", "op_dia": 1.4,
 "dentro": {"n": 512, "R": 0.08, "ic95": [-0.05, 0.21]},
 "fuera": {"n": 330, "R": 0.21, "ic95": [0.04, 0.38], "p": 0.003, "q": 0.07, "dsr": 0.97, "mitades": [0.18, 0.24]},
 "avance_trimestral": {"R": 0.12, "positivos": "4 de 5"},
 "robustez": {"base_p": 0.01, "sin_5_mayores": 0.1, "sin_mejor_semana": 0.15, "desliz_x2": 0.12, "entrada_1_min": 0.18,
              "estabilidad": "4 de 6", "sin_xperps": -0.05, "objetivo_2pb": 0.19, "solo_largos": 0.09},
 "cuenta": {"R_dia": 0.29, "pct_dia": 0.145, "pct_dia_compuesto": 0.144, "caida_max_pct": 8.2},
 "meta": {"falta_R_dia": 1.71, "falta_pct_dia": 0.855, "texto": "Le faltan 1,71 R al día (0,86 % de la cuenta)…"},
 "avance": {"desde": 0, "completas": 12, "abiertas": 2, "R_medio": 0.3, "p_holm": null, "caida_max_pct": 1.2,
            "faltan": "28 operaciones y 21 días", "curva": [[0, 0.0]]},
 "checks": {"R1": true, "K4": true, "B7": true}, "motivos": [], "puntuacion": 0.04,
 "reglas": {"cumple": true,
            "excepcion_tiempo": {"tope_sin_activar_h": 24, "fuera": {"n": 333, "de": 898, "pct": 37.1, "entre": 320,
                                                                    "ganan_menos_2R": 229},
                                 "texto": "Cierra por tiempo a las 24 h si no ha llegado a la ganancia mínima…"}}}
```

`reglas.cumple` = las de `evolucion.cumple_reglas` (stop, 2R, 3 % neto, sin break-even). `reglas.excepcion_tiempo`
(null en las fuera de reglas) = el cierre por tiempo antes de la ganancia mínima (6.5); `fuera` sale de
`metricas.fuera.cierres_por_tiempo` (solo en las medidas desde el 29-10-2026).

`mejores` ordena por `puntuacion` = límite inferior del IC 95 % del R medio fuera (remuestreo por días). No ordena por
el R medio a secas, que premia la suerte. Incluye las que no han pasado, con su motivo («no ha pasado: B1, p 0,21»).

### 13.4 `cola.json` e `hipotesis.json`

- `cola.json` lleva, por tipo, los pendientes, los en curso, los pausados y los fallidos de las últimas 24 h, y los 50
  siguientes.
- `hipotesis.json` es el índice de las 1.000 últimas: id, título, familia, autor, recibida, estado, mejor clase y motivo.
- `hipotesis\<id>.json` es el detalle:
  - la hipótesis tal cual llegó y su admisión;
  - cada variante con su tarjeta y sus pruebas;
  - las 200 últimas operaciones fuera y todas las de avance, para la gráfica.

### 13.5 `para_el_generador.json`, `para_el_generador_estado.json` y `ya_probado.json` (los lee el agente generador)

**El generador no ve el fuera de muestra** (revisión del 29-10-2026). Con el detalle (por moneda, año, trimestre, solo
largos, sin X-Perps, las mitades y las últimas operaciones) propondría el recorte que mejor salió fuera, por ejemplo
«solo largos en SOL»: esa variante contaría 1 en N, pero se habría elegido entre decenas de cortes ya vistos de los
mismos 411 días (el jardín de senderos que se bifurcan). Por eso:
- **`ya_probado.json`**, de cada variante: firma, parámetros, salida, clase, `etapa` a la que llegó, `motivo` y sus
  cifras **dentro** de muestra (`dentro`: n y R de la etapa rápida). Los motivos de la admisión (A) y de la rápida (R),
  tal cual; los del fuera de muestra y del avance (K, B y V), solo el código y un texto sin cifras
  (`salida_web.TEXTO_CODIGO`: «K2: R medio fuera de muestra ≤ 0»).
- **`para_el_generador.json`**: lo mismo en `descartes_recientes`; las lecciones (cuántas pasan cada etapa y el código
  que más falla); `no_leer` (la lista de abajo) y `estado_al_dia`.
- **`para_el_generador_estado.json`** (con cada `estado.json`): `estado`, `motivo`, `latido`, `canario`, el cupo de hoy
  y la próxima pausa. Es lo único del estado que necesita.
- **No lee** `estado.json`, `mejores.json`, `hipotesis.json`, `hipotesis\`, `propuestas\`, `trabajos\`, `lab_vivo.db`
  ni `primera_tanda.md`: llevan las tarjetas con el fuera de muestra.
- **Sí puede leer** los diarios (sin cifras del fuera de muestra) y las cifras de la prueba en avance que salgan en
  ellos: son datos nuevos, y una idea nueva se mide en un avance posterior.

Forma de `para_el_generador.json`:

```json
{"formato": 1, "ts": 0, "estado": "activo", "criterios_version": 1,
 "reglas_usuario": {"stop_min_pct": 1.5, "rr_min": 2.0, "neto_min_pct": 3.0, "riesgo_pct": 0.5, "max_entradas_dia": 6,
                    "max_riesgo_dia_pct": 3.0, "largos": "contado", "cortos": "perpetuo", "sin_break_even": true},
 "cupo": {"pasada": 6, "hoy_libres": 18, "por_familia_hoy": {"ruptura_canal": 2}},
 "familias": [{"nombre": "ruptura_canal", "version": 1, "titulo": "…", "momento": "continuo", "historia": true,
               "datos": ["velas_1m"], "parametros": {"horas": {"tipo": "int", "opciones": [24, 48, 96, 168], "defecto": 48}},
               "probadas": 6, "por_etapa": {"rapida": 4, "completa": 0}, "fallo_mas_comun": "K2"}],
 "salidas": {"A": {"…": "…"}, "H": {}, "C": {}},
 "datos": [{"clave": "velas_1m", "desde": "2021-01", "hasta": "…", "historia": true},
           {"clave": "noticias_historia", "dias": 91, "historia": false}],
 "umbral": {"n_dsr": 4300, "z": 3.65, "texto": "Con N efectivo ≈ 4.300, una estrategia necesita un Sharpe diario ≈ 0,25 fuera de muestra"},
 "lecciones": ["…"], "candidatas": [], "en_avance": [], "descartes_recientes": [{"id": "…", "motivo": "…"}],
 "lo_que_no": ["E2 (impulso de 15 min con filtros): descartada por el usuario el 29-10-2026",
               "Entrar en las 4 monedas en cada apertura sin filtro («6 diarias»): pierde",
               "Medios de cripto como señal de sentido: no anticipan (docs/05, sección 22)",
               "Objetivos < 2R o < 3 % neto, stops < 1,5 %, break-even, cierres a las 4 h",
               "Efectos de pocos pb (estacionalidad horaria, desfases de minutos): no pagan 0,24 % de costes con objetivos ≥ 3 %",
               "GDELT y ratio largo/corto como historia: no hay datos"]}
```

`ya_probado.json` lleva las firmas y los parámetros de todas las variantes probadas, por familia, con su clase, su etapa,
su motivo (sin cifras del fuera de muestra) y sus cifras dentro de muestra. Así no se repite nada.

### 13.6 `propuestas\<id>.json` (las lee la evolución) y `decisiones\<id>.json` (las escribe el servidor)

```json
{"formato": 1, "id": "lv-<firma>-20261212-043012", "ts": 0, "creada": 0, "tipo": "estrategia_lab_vivo",
 "estado": "pendiente|aceptada|descartada|retirada",
 "variante": "h-…:V2", "hipotesis": "h-…", "titulo": "…", "familia": "…", "regla": "…", "criterios_version": 1,
 "tarjeta": {"…": "la de 13.3"},
 "reglas": {"cumple": true, "excepcion_tiempo": {"…": "la de 13.3"}},
 "requiere": ["acepta_excepcion_tiempo"],
 "como_operarla": {"motor": false, "params_motor": null,
                   "texto": "Necesita un modelo nuevo en el motor de aperturas (lo programa el coordinador). Hasta entonces sigue en la prueba en avance."},
 "aviso": "Propuesta del laboratorio: no es una ventaja garantizada. Si la aceptas, pasa a la cartera paper (dinero ficticio) cuando esté programada; nada se activa sin ti.",
 "motivo_retirada": "solo si está retirada", "falta": "solo si una aceptación llegó incompleta"}
```

```json
{"formato": 1, "propuesta": "lv-…", "decision": "aceptada|descartada", "ts": 0, "por": "usuario", "nota": "…",
 "acepta_excepcion_tiempo": true}
```

- **El id es de una propuesta, no de la variante** (`lv-<firma>-<día>-<hora>`): no se reutiliza. Al proponer otra vez
  una variante, sus propuestas pendientes anteriores pasan a «retirada».
- **Una aceptación solo vale** si la variante sigue «propuesta» (no revocada ni descartada en avance desde entonces), si
  es la última propuesta de esa variante y, si la propuesta lleva `requiere: ["acepta_excepcion_tiempo"]`, si la
  decisión trae `acepta_excepcion_tiempo: true`. Si no:
  - variante que ya no está propuesta o propuesta vieja: la propuesta pasa a «retirada» con el motivo
    («aceptación no válida: …») y se apunta en `eventos`;
  - sin aceptar la excepción: sigue «pendiente», con `falta`, hasta que llegue la decisión completa.
- **`ts` de la decisión, en ms.** Una decisión anterior a la propuesta (`ts` < `creada`) es de otra y no se aplica.
- **Una propuesta retirada** ya no se puede aceptar: la evolución tiene que quitarla de la pestaña (o enseñarla como
  retirada, con su motivo).
- **Una propuesta descartada** no se vuelve a proponer sin datos de avance posteriores a la decisión. Es lo mismo que
  hace `evolucion.py` con lo que el usuario descarta.
- **Una aceptada** queda «aceptada». Su programación en el motor es otra tarea, del coordinador.

## 14. Contrato de cableado para el coordinador

### 14.1 Servidor (`bot/server.py`, cuando el otro flujo lo suelte)

**No importa `bot.lab_vivo`:** solo lee archivos de `config.DATA_DIR / "lab_vivo"`.
- Los lee en un hilo, con caché por `mtime`.
- Valida los parámetros: `id` con `^h-\d{8}-\d{4}-[a-z0-9_-]{3,60}$` (nada de rutas) y `dia` con `^\d{4}-\d{2}-\d{2}$`.
- Un parámetro no válido da 400.

**Rutas:**

| Ruta | Devuelve |
|---|---|
| `GET /api/lab_vivo` | `estado.json` más `mejores` (de `mejores.json`, hasta 20) y `diario` (el de hoy). Sin archivo: `{"estado": "apagado", "nota": "El laboratorio vivo no está en marcha (scripts/lab-vivo.ps1)"}`. Si `latido` es de hace más de 3 min: `estado` = `"parado"` y el resto, el último que hubo. |
| `GET /api/lab_vivo/hipotesis?estado=&familia=&limite=100` | `{hipotesis}` de `hipotesis.json`, filtradas (`limite` de 1 a 1.000) |
| `GET /api/lab_vivo/hipotesis?id=` | `hipotesis\<id>.json`; 404 si no existe |
| `GET /api/lab_vivo/diario?dia=AAAA-MM-DD` | El diario de ese día (por defecto, hoy); 404 si no hay |
| `GET /api/lab_vivo/cola` | `cola.json` |
| `GET /api/lab_vivo/generador?limite=10` | Las últimas notas de `generador\` |

No van en el estado inicial del WebSocket, salvo el mensaje de 14.2.

### 14.2 Mensaje del bus `lab_vivo`

- **Es un estado, no un evento.** Una tarea del servidor mira el `mtime` de `estado.json` cada 30 s y, si cambió, publica
  un resumen:

```json
{"type": "lab_vivo", "ts": 0, "estado": "activo", "motivo": "…", "latido": 0, "aviso": "…", "proxima_pausa": {},
 "contadores": {}, "embudo": [], "meta": {}, "mejores": [], "en_avance": [], "listas": [],
 "canario": "ok", "recursos": {"procesos": 1, "memoria_libre_mb": 2048}}
```

- Las listas van recortadas a 5. Las tarjetas, sin `curva` ni `checks`.
- Sin archivo: `{"type": "lab_vivo", "estado": "apagado"}` una sola vez.

### 14.3 Mando desde la web (WebSocket `/events`, con el origen estricto de siempre)

| Mando | Efecto |
|---|---|
| `{"type": "lab_vivo", "accion": "pausar"}` | Crea `pausa_manual` |
| `{"type": "lab_vivo", "accion": "reanudar"}` | Lo borra |

El resultado vuelve en `orden_web`. No hay más mandos: ni cambiar criterios, ni cupos, ni aprobar nada desde aquí.

### 14.4 Pestaña «Laboratorio» de la web (`#laboratorio`)

Visual y legible de un vistazo, como pide el usuario:

| Parte | Qué enseña |
|---|---|
| Cabecera | Estado con cuenta atrás de la pausa («Pausa: apertura de Europa, vuelve a las 09:30»), procesos y memoria, botón «Pausar / Reanudar» y el aviso «todo es virtual: nada opera» |
| Meta del 1 % | Medidor con lo mejor en R al día (fuera de muestra y en avance) frente a los 2 R; «le faltan …»; la nota fija «con 0,5 % de riesgo por operación (no se sube)» |
| Embudo | De recibidas a listas, hoy y en total; el número de pruebas acumulado y el listón actual («hace falta un Sharpe diario ≈ 0,25») |
| Mejores | Tabla de tarjetas: título, familia, clase, operaciones al día, R fuera con su IC, R al día, % al día, lo que falta y el motivo si no pasa. Al pulsar, el detalle con su curva de R (fuera de muestra y avance, marcados) y sus comprobaciones en verde o rojo. |
| En avance | Tarjetas con días, operaciones completas y abiertas, R medio, p de Holm, caída máxima, lo que falta («28 operaciones y 21 días») y una minicurva |
| Diario | Línea de tiempo: hoy y los anteriores, con las notas del generador |
| Cola y qué falla más | Lo que viene y los motivos de descarte más comunes |
| Demostración (sin bot) | Datos de muestra incrustados (`<script type="application/json" id="labVivoData">`), marcados «demostración» |

### 14.5 Evolución (`bot/evolucion.py`)

- Cada `TICK_S` lee `propuestas\*.json` con `estado: "pendiente"`.
- Crea una fila de `evolution_proposals` de tipo nuevo `estrategia_lab_vivo`, con `clave` = id de la propuesta.
  - No la crea si ya existe o si hay una descartada de la misma variante sin datos de avance posteriores.
  - Su `cuerpo` es la propuesta entera.
  - **Si la propuesta pasa a `retirada`** (revocada con el N de hoy, descartada en avance, sustituida por otra o una
    aceptación no válida), la fila se cierra con su `motivo_retirada`: ya no se puede aplicar.
- Al aplicarla o descartarla, el usuario lo hace desde la pestaña Evolución, como siempre.
  - **Con `requiere: ["acepta_excepcion_tiempo"]`** la pestaña enseña `reglas.excepcion_tiempo.texto` al lado del botón
    y pide aceptarla aparte («Acepto que cierre por tiempo entre −1R y +2R, como «6 diarias»»). Sin esa casilla, la
    decisión «aceptada» no se aplica (queda pendiente con `falta`).
- Escribe `decisiones\<id>.json` (13.6, con `ts` en ms y `acepta_excepcion_tiempo`) y apunta la entrada en
  `evolution_log`, con `origen` `lab_vivo`.
- **«Aplicar»:**
  - Con `como_operarla.motor = true` (familia `apertura_impulso`): puede escribir `estrategia_activa.json` con
    `params_motor`, tras `cumple_reglas`, por el camino de siempre.
  - Con `motor = false`: queda **pedida** y no se activa nada.
- El modo «automático» de la evolución **nunca** aplica una propuesta del laboratorio.

### 14.6 Documentación y arranque

**Texto para `docs/04-contratos.md`:**
- En 1: «`bot/lab_vivo/` va en un proceso aparte (docs/07); el servidor solo lee sus archivos».
- En 2: 2.19 `lab_vivo` (el mensaje de 14.2).
- En 3: las rutas de 14.1 y el mando de 14.3.
- La base `lab_vivo.db` no está en `bot.db`: se documenta aquí (apartado 12).

**`CLAUDE.md`:**
- En «Estructura»: `bot/lab_vivo/` (laboratorio vivo, proceso aparte) y `docs/07-laboratorio-vivo.md`.
- En «Entornos»: el arranque con `scripts/lab-vivo.ps1`.

**Arranque al iniciar sesión.** Lo instala el usuario, o el coordinador con su permiso, porque es configuración
persistente del PC. Puede ser un acceso directo en «Inicio» o una tarea de Windows «al iniciar sesión» con:
`powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "M:\OneDrive\Bot de trading fundamental\scripts\lab-vivo.ps1"`.

**Tarea programada del generador.** La crea el coordinador con el visto bueno del usuario (apartado 15), porque gasta
tokens de Claude.

## 15. El agente generador

### 15.1 Qué es y cuándo corre

- **Qué es.** Una **tarea programada local** del escritorio de Claude Code, en el PC del usuario. No es una rutina en la
  nube: allí no están los datos ni `lab_vivo`.
- **Cuándo.** Para empezar, **2 pasadas al día: 07:15 y 17:30** de Madrid (fuera de las pausas en invierno y en
  verano), 12 hipótesis como mucho. Se puede subir a 4 (07:15, 12:15, 17:30 y 22:30: el cupo entero de 24) si el
  usuario lo quiere y el gasto lo permite. Más hipótesis no acercan la meta: suben el N y el listón de todas (2.2).
- **Gasto previsto.** Unos 60.000-90.000 tokens por pasada si lee solo lo que dice 15.2 (las partes de este documento,
  `para_el_generador.json` ≈ 25.000, lo de `ya_probado.json` de sus familias, los diarios y sus notas) y escribe ≤ 6
  hipótesis. Con 2 pasadas, ≈ 150.000 al día; con 4, ≈ 300.000. Una familia nueva con sus pruebas, más.
- **Dónde trabaja.** Directorio de trabajo: el proyecto.
- **Por qué Claude.** Gasta tokens, pero escribir hipótesis con fundamento es análisis crítico, que es donde el usuario
  quiere usar Claude. El trabajo continuo (medir) va sin IA.

**Permisos mínimos** (los fija el coordinador en la tarea):

| Permiso | Dónde |
|---|---|
| Leer | El proyecto y `docs/`; en `M:\BotFundamental-datos\lab_vivo\`, solo `para_el_generador.json`, `para_el_generador_estado.json`, `ya_probado.json`, `diario\` y `generador\` (13.5: nada del detalle del fuera de muestra) |
| Escribir | Solo `lab_vivo\cola\entrada\`, `lab_vivo\generador\`, `bot/lab_vivo/familias/<nueva>.py` y `tests/test_lab_vivo_familia_<nueva>.py` |
| Ejecutar | `python -X utf8 -m bot.lab_vivo validar …` y `python -X utf8 -m pytest -q tests/test_lab_vivo_familias.py tests/test_lab_vivo_familia_<nueva>.py` |
| Red | Solo para leer documentación pública (artículos, libros, estrategias conocidas) |

### 15.2 Protocolo de cada pasada

1. **Leer.**
   - `para_el_generador_estado.json` (si toca proponer y el cupo), `para_el_generador.json`, `ya_probado.json` y el
     diario de hoy y el de ayer.
   - Sus dos últimas notas (`generador\`).
   - Este documento (5, 6, 8, 10 y 15) y `CLAUDE.md`.
   - **Nunca** `estado.json`, `mejores.json`, `hipotesis.json`, `hipotesis\`, `propuestas\` ni `lab_vivo.db` (13.5).
2. **Si no toca proponer** (estado `canario_fallido`, `error`, `apagado` o `parado`, o un `latido` de hace más de 3 min):
   apuntar la nota y terminar.
3. **Pensar antes de escribir.** Por cada idea:
   - el mecanismo en dos frases (quién está al otro lado y por qué se repite);
   - la fuente;
   - por qué valdría en cripto hoy y con estos costes;
   - qué la desmentiría;
   - en qué se diferencia de lo probado (`ya_probado.json`);
   - si el movimiento que busca es compatible con objetivos ≥ 3 % neto y stops ≥ 1,5 %: movimientos de horas o días, no
     de pocos pb.

   Si falta algo, la idea no se propone.
4. **Escribir.** Como mucho el cupo de la pasada (6) en `cola\entrada\<id>.json`:
   - escribir `.tmp` y renombrar;
   - ≤ 6 variantes y ≤ 3 filtros, salida del catálogo;
   - variantes que cambien una sola cosa cada una respecto a la primera, para que la estabilidad (B6) tenga sentido.
5. **Validar.** `python -X utf8 -m bot.lab_vivo validar <archivo>`. Si falla, corregir o quitar la hipótesis.
6. **Familia nueva** (solo si ninguna familia expresa la idea):
   - Escribir `bot/lab_vivo/familias/<nombre>.py` con `version = 1`. Solo puede importar `bot.lab_vivo.familias.base`,
     `bot.lab_vivo.indicadores`, `bot.lab_vivo.filtros`, numpy y la biblioteca estándar.
   - Escribir `tests/test_lab_vivo_familia_<nombre>.py`: velas sintéticas en las que la señal esperada se sabe a mano, una
     prueba del recorte propia y ninguna red.
   - Pasar las pruebas. **Si no pasan, la familia se borra y la hipótesis no se envía.**
   - La familia queda sin commit: el usuario o el coordinador la revisa y la sube.
7. **Apuntar la pasada** en `generador\AAAA-MM-DD-HHMM.json`: qué leyó, qué propone y por qué, qué descartó antes de
   enviar y archivos nuevos.

### 15.3 Lo que no puede hacer

- **Medir resultados por su cuenta.** Ni backtests, ni scripts que miren el precio después de una señal. Solo mide el
  corredor, y todo lo que mide cuenta en N. Una prueba hecha a escondidas sería un N oculto. `validar` no mide
  resultados: solo formato, reglas, duplicados, recorte y frecuencia de señales.
- **Editar código, configuración o criterios.** Ningún archivo existente del bot, de `bot/lab_vivo/`, de `tests/` ni de
  `docs/`: solo crea los suyos nuevos. Tampoco `config.py`, `criterios.py` ni los cupos.
- **Tocar el bot o la cartera.** Ni `bot.db`, ni `estrategia_activa.json`, ni parar o reiniciar el bot o el corredor.
- **Saltarse las reglas o el riesgo.** Ni subir el riesgo, ni proponer fuera de reglas sin marcarlo.
- **Hacer commits o push.**
- **Perseguir el 1 % retocando lo que casi llega.** Lo que casi llega se mide en avance, no se repite con otro número.
- **Mirar el fuera de muestra para proponer.** Ni `estado.json`, ni `mejores.json`, ni `hipotesis\` (13.5). Un recorte de
  lo que salió bien fuera («solo largos», «solo SOL», «sin 2025») ya no tiene un fuera de muestra con el que probarse.
- **Volver a proponer E2** (descartada por el usuario) ni lo de `lo_que_no`.

### 15.4 Ideas con fundamento para empezar (no son resultados: hay que medirlas)

| Idea | Mecanismo | Fuente | Encaje con las reglas |
|---|---|---|---|
| Momento de series temporales (días y semanas) | Infra-reacción y flujos que persiguen la tendencia; en cripto, entradas de capital en oleadas | Moskowitz, Ooi y Pedersen (2012); Hurst, Ooi y Pedersen (2017); Liu y Tsyvinski (2021) | Movimientos de días; cerrojo para dejar correr; pocas operaciones por moneda |
| Ruptura de canal de varios días | Stops y órdenes de ruptura acumulados más allá de los extremos; liquidaciones en cascada | Faith (2007), las «tortugas» | Stop al otro lado o por ATR, salida A o C |
| Compresión y expansión | La volatilidad se agrupa y vuelve a su media: tras rangos estrechos vienen amplios | Crabel (1990); Bollinger (2001) | Stop al otro lado del rango estrecho |
| BTC lidera y las alts siguen | El precio se descubre en BTC; las alts, con beta > 1, recorren más y tarde | Literatura de adelanto y retraso entre criptomonedas | Objetivos de 3-6 % plausibles en SOL y XRP |
| Rebote tras capitulación | Las liquidaciones forzosas exageran el movimiento; con volumen extremo, el suelo | Reversión a corto plazo (Jegadeesh, 1990; Lehmann, 1990) | Solo largos al contado; selectiva |
| Funding extremo con giro de precio | Los apalancados que pagan funding muy alto se ven forzados a cerrar | Práctica conocida del mercado de perpetuos | En aperturas ya falló como sentido (`fund_contra`); 24/7 con confirmación es otra idea |
| Ruptura del rango de apertura con contexto | El flujo de las aperturas de bolsa; ruptura del rango inicial | Crabel (1990); Zarattini, Barbon y Aziz (2023), en acciones de EE. UU. | Ya medido sin contexto (laboratorio, Ruptura V3): solo con una idea nueva (compresión previa, régimen) |
| Noticias oficiales y de datos | Docs/05 (sección 22): los medios de cripto no anticipan; quedan las fuentes oficiales (SEC, bancos centrales, exchanges, ETF) | Docs/05, sección 22 | Solo exploratoria, en avance (8.6) |

**Autorrevisión.** La skill `edge-strategy-reviewer` necesita PyYAML, que no está en el entorno y no se añade. Sus
criterios se aplican a mano y se escriben en `logica.revision`:
- plausibilidad;
- sobreajuste (filtros y umbrales raros);
- muestra;
- régimen;
- salida;
- concentración;
- ejecución;
- falsación.

### 15.5 Texto de la tarea programada (para pegar)

**Frecuencia:** 2 pasadas al día, a las 07:15 y a las 17:30 de Madrid, para empezar; 4 (07:15, 12:15, 17:30 y 22:30) si
el usuario lo quiere y el gasto lo permite (15.1). Directorio de trabajo: el proyecto.

> Eres el generador del laboratorio vivo del proyecto «Bot de trading fundamental» (M:\OneDrive\Bot de trading
> fundamental). Todo es paper y se escribe en español. Tu trabajo es proponer pocas hipótesis buenas, no muchas: cada
> una que se mide sube el listón de todas.
>
> **1. Lee solo esto:**
> - `CLAUDE.md` y `docs/07-laboratorio-vivo.md`, apartados 5, 6, 8, 10 y 15.
> - En `M:\BotFundamental-datos\lab_vivo\`: `para_el_generador_estado.json`, `para_el_generador.json`, los diarios de
>   hoy y de ayer (`diario\`) y tus dos últimas notas (`generador\`).
> - De `ya_probado.json`, solo las variantes de las familias que vayas a usar (búscalas; no lo leas entero).
> - El código de las familias que vayas a usar (`bot/lab_vivo/familias/<nombre>.py`).
>
> **No leas** `estado.json`, `mejores.json`, `hipotesis.json`, `hipotesis\`, `propuestas\`, `trabajos\`, `lab_vivo.db`
> ni `primera_tanda.md`: llevan el detalle del fuera de muestra. Una idea sacada de ahí (por ejemplo, «solo largos en
> SOL» porque fue lo que mejor salió) se elige entre muchos cortes de los mismos 411 días, y su fuera de muestra ya no
> prueba nada.
>
> **2. Si no toca proponer** (en `para_el_generador_estado.json`: estado `canario_fallido`, `error`, `apagado` o
> `parado`, un `latido` de hace más de 3 min o `cupo.hoy_libres` = 0), escribe la nota y termina.
>
> **3. Propón como mucho `cupo.pasada` hipótesis** (y no más de `cupo.hoy_libres`), solo con lógica aplastante. Cada una
> con:
> - el mecanismo (quién está al otro lado y por qué se repite), en dos frases;
> - al menos una fuente pública: un artículo, un libro o una estrategia conocida que funcione hoy;
> - por qué valdría en cripto hoy con 0,24 % de costes y objetivos ≥ 3 % neto (movimientos de horas o días, no de pocos
>   pb);
> - la predicción y lo que la desmentiría;
> - en qué se diferencia de lo ya probado (`ya_probado.json`: firma, etapa y código del motivo).
>
> Nada al tuntún: si no puedes explicar el mecanismo en dos frases, no la propongas. De lo probado solo sabes a qué
> etapa llegó y por qué cayó (el código): úsalo para no repetir, no para ajustar.
>
> **4. Reglas del usuario** (intocables):
> - Stop ≥ 1,5 %; toda parte que gana ≥ 2R y ≥ 3 % neto; sin break-even ni nada que mueva el stop antes.
> - Largos al contado y cortos en perpetuo.
> - Una posición por moneda; ≤ 6 entradas y 3 % de riesgo al día; 0,5 % de riesgo por operación, que no se sube.
> - Salidas solo del catálogo (A, H, C u OBJ). Todas cierran por tiempo antes de la ganancia mínima: es la excepción de
>   «6 diarias», y el usuario tendrá que aceptarla en cada propuesta (6.5). Elige el tope de tiempo por el movimiento que
>   buscas, no por una cifra.
> - ≤ 6 variantes fijadas ahora, cada una cambiando una sola cosa respecto a la primera, y ≤ 3 filtros.
>
> **5. Escribe y valida.** Cada hipótesis en `cola\entrada\<id>.json` (escribe `.tmp` y renombra), con `autor`
> «generador-claude» y el id `h-AAAAMMDD-HHMM-<nombre>` de esta pasada. Valídala con
> `M:\BotFundamental-runtime\venv\Scripts\python.exe -X utf8 -m bot.lab_vivo validar <archivo>`. Si dice «NO VALE»,
> corrígela o retírala. Si avisa de una pausa de las aperturas, vale igual: A8 y A9 los hará el corredor.
>
> **6. Si la idea necesita una familia nueva:**
> - Escribe `bot/lab_vivo/familias/<nombre>.py` (version 1, solo con `base`, `indicadores`, `filtros`, numpy y la
>   biblioteca estándar, sin red ni archivos; la señal en t, solo con lo que se conoce en t: velas que abren antes de t,
>   barras cerradas y funding con sello ≤ t) y `tests/test_lab_vivo_familia_<nombre>.py` (velas sintéticas, la señal
>   sabida a mano y un corte en cada señal con `bot.lab_vivo.recorte.comprobar`).
> - Pasa `M:\BotFundamental-runtime\venv\Scripts\python.exe -X utf8 -m pytest -q tests/test_lab_vivo_familias.py
>   tests/test_lab_vivo_familia_<nombre>.py`.
> - Si no pasan, borra la familia y su prueba y no envíes la hipótesis.
>
> **7. Prohibido:**
> - Medir resultados por tu cuenta (backtests o scripts que miren el precio después de una señal): solo mide el
>   corredor, y todo cuenta en el número de pruebas.
> - Mirar el fuera de muestra (los archivos del punto 1).
> - Editar archivos que ya existen, la configuración, los criterios o los cupos.
> - Escribir en `bot.db` o en `lab_vivo.db`, tocar `estrategia_activa.json`, y parar o reiniciar el bot o el corredor.
> - Hacer commits.
> - Volver a proponer E2 o lo de `lo_que_no`.
>
> El 1 % diario es una meta que se informa, no un filtro: no retoques lo que casi llega para que cruce la línea.
>
> **8. Al acabar,** escribe tu nota en `generador\AAAA-MM-DD-HHMM.json`: `resumen`, `leido`, `propuestas` (cada una con
> su porqué), `descartadas` (antes de enviar, y por qué) y `archivos` nuevos.

## 16. scripts/lab-vivo.ps1

**Arranque y vigilante**, con la forma de `scripts/vigilante-bot.ps1`.
- **Uso:** `powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File scripts\lab-vivo.ps1
  [-Procesos 2] [-Cpus "3,4,5"] [-MemoriaMB 2500] [-Prueba]`.
- **Uno solo:** mutex `Global\RadarLabVivoVigilante`. Si ya hay otro vigilante, sale.
- **Registro:** `lab_vivo\registro\vigilante.log`.
- **Bucle cada 60 s:**
  1. Si existe `M:\BotFundamental-datos\lab_vivo\parar`: espera hasta 2 min a que el corredor salga solo (también lo ve),
     lo mata si sigue y termina.
  2. **Vivo** = el PID de `corredor.lock` es **de verdad el corredor** y `estado.json` tiene `latido` de hace < 3 min.
     «De verdad» (`CorredorDelLock`, con `Get-CimInstance Win32_Process`): su línea de órdenes lleva `bot.lab_vivo
     correr` y su `CreationDate` coincide con el `creado` del lock (± 2 s; un lock antiguo sin `creado`: el proceso es
     anterior a su `ts`). El lock solo se borra al salir bien: tras un reinicio, un apagón o un `taskkill` se queda, y
     Windows reutiliza los PID. Un lock cuyo PID es ahora de otro proceso se apunta («lock viejo») y **no se toca nunca**:
     ni se mata ni se le cambia la prioridad (podría ser el bot, llama-server, explorer o el vigilante del bot). El
     corredor lo reemplaza al arrancar.
  3. Con 2 fallos seguidos y sin corredor, lo arranca:
     - `Start-Process -FilePath M:\BotFundamental-runtime\venv\Scripts\python.exe -ArgumentList "-X","utf8","-m","bot.lab_vivo","correr" -WorkingDirectory <proyecto> -WindowStyle Hidden -PassThru`.
     - Variables de entorno: `TMP`/`TEMP` en `lab_vivo\tmp` y los hilos de BLAS en 1 (11.6).
     - Después, `PriorityClass = 'BelowNormal'` y `ProcessorAffinity` con la máscara de `-Cpus`. El corredor también se
       los pone él mismo.
  4. Si el corredor (verificado) vive pero el latido tiene > 10 min (colgado), `taskkill /PID <pid> /T /F` y vuelta a
     arrancar. `taskkill /T` solo se lanza sobre un corredor verificado.
- **`-Prueba`:** dice qué haría (una vuelta), sin arrancar ni parar nada. **`-Lab <carpeta>`:** otra carpeta del
  laboratorio, solo para las pruebas (`tests/test_lab_vivo_vigilante.py`), con su propio mutex.
- **Nunca toca el bot** (otro proceso, otro vigilante).

## 17. Pruebas

En `tests/`, sin red, con velas sintéticas y temporales en M: (`conftest.py`). `conftest.py` pone `BOT_USER_RULES=0`:
las pruebas que necesitan las reglas las activan con `monkeypatch.setattr(config, "USER_RULES", True)`.

| Archivo | Qué comprueba |
|---|---|
| `test_lab_vivo_hipotesis.py` | Formato y campos; firma canónica (los valores por defecto cuentan, el orden de las listas no); duplicados; `cumple_reglas` por variante con `evolucion.cumple_reglas`; plan imposible (H 2R con stop 1,5 %); fuera de reglas marcada; ≤ 6 variantes y ≤ 3 filtros; buzón atómico (un `.tmp` no se lee); motivos de rechazo en español |
| `test_lab_vivo_libro.py` | El libro da lo mismo que `simulate_plan` llamado a mano. También: una posición por moneda (con la mitad que queda); topes del día de Madrid (6 entradas y 3 %); controles del paper (pérdida del día, 4 posiciones, exposición, perpetuos ≤ 2 ×); funding en los cortos (la mitad tras la parcial); comisiones por perfil (tomador y devolución del creador en la parcial y el objetivo); sin X-Perps no hay cortos; stop estructural subido a 1,5 % y veto por encima de 5 %; «fin de datos» que ocupa y no cuenta. Y unas cifras fijas de un escenario sintético calculado con el `Book` de `medir_selectiva.py`. |
| `test_lab_vivo_familias.py` | La conformidad de 6.4 para todas las familias registradas y la guarda sin red |
| `test_lab_vivo_familia_<nombre>.py` | Una por familia: la señal esperada en velas hechas a mano y su propia prueba del recorte |
| `test_lab_vivo_medidas.py` | t HAC y p; Sharpe deflactado con N acumulado; BH con previas a p = 1; N efectivo; base emparejada con un sentido conocido; sin 5 mayores y sin la mejor semana; avance trimestral con elección hacia atrás; estreses; clases de frecuencia; meta del 1 % (2 R al día = 1 %); Holm acumulado |
| `test_lab_vivo_corredor.py` | Prioridades, turno entre familias y cupos; pausas con `openings_on` (se juntan; 5 min antes y 30 después; festivos y fines de semana; modo prudente sin agenda); ningún trabajo empieza en pausa; parada cooperativa y forzada; reanudación (`en_curso` vuelve a la cola desde su punto; ingesta idempotente; `.tmp` a medias); tres fallos; guarda de memoria simulada; freno por CPU; cambio de época; `parar` y `pausa_manual`; canario fallido que para la medida; familia cambiada sin subir versión; código del bot que no se importa |
| `test_lab_vivo_almacen.py` | El esquema; los disparadores de solo añadir en `pruebas`, `series` y `eventos`; N total contando una vez por variante |
| `test_lab_vivo_salida_web.py` | La forma de `estado.json`, el diario, la tarjeta, `para_el_generador.json`, las propuestas y la lectura de `decisiones\`; los textos en español y los números como números |

**El canario con datos reales no va en la batería**, porque necesita la caché de 3 años. Va en
`python -m bot.lab_vivo canario`, y el implementador lo pasa una vez y lo apunta.

## 18. Plan por fases y criterios de aceptación

| Fase | Qué | Aceptación |
|---|---|---|
| **1. Núcleo** | `ajustes`, `criterios`, `almacen`, `datos` (caché por época), `indicadores`, `filtros`, `salidas`, `libro`, `medidas`, `hipotesis`, `etapas`, `agenda`, `recursos`, `corredor`, `salida_web`, `canario`, `familias/base` y las 4 familias iniciales; `scripts/lab-vivo.ps1`; la siembra de 6.7; todas las pruebas de 17 | `pytest -q` en verde. El canario del libro reproduce `seis2_resumen.json`. Un día entero de corredor con la cola de la siembra, sin una sola tarea en las pausas (registro), con ≤ 2.500 MB y BELOW_NORMAL (comprobado con el Administrador de tareas). Las salidas de 13 cumplen el esquema. |
| **2. Más familias y generador** | El adaptador `laboratorio` y su canario (E2); `lider_btc`, `reversion_capitulacion`, `funding_extremo` y `tendencia_retroceso`; las series de los 2.000 para el N efectivo conjunto; la tarea programada del generador (con el visto bueno del usuario) | Una pasada del generador escribe, valida y apunta sus notas. Ninguna escritura fuera de lo permitido. |
| **3. Cableado** (coordinador) | Rutas, mensaje, mando, pestaña Laboratorio, propuestas en Evolución, `docs/04` y `CLAUDE.md` (apartado 14) | Las pruebas de la web (`tests/test_radar_web.py`) y del servidor. Con el laboratorio apagado, todo da «apagado» sin errores. |
| **4. Noticias y ejecución** | `noticias_vivas` (exploratoria; el corredor guarda `GET /api/noticias` cada 10 min fuera de las pausas en `noticias_vivas`). Si el usuario acepta una propuesta que no es `apertura_impulso`, su modelo en el motor. | Lo decide el usuario |

## 19. Decisiones del usuario y riesgos

**Lo que tiene que aprobar el usuario:**
1. Los criterios v1 de los apartados 8 y 10. Están fijados hoy, antes de medir nada; cambiarlos después exige una versión
   nueva y no vale hacia atrás.
2. El cupo de 24 hipótesis al día (≤ 144 variantes).
3. Los recursos: hasta 2 procesos de cálculo a la vez (el segundo solo con ≥ 4 GB libres; 3 como mucho si se sube), 3
   CPU, 2.500 MB y prioridad baja.
4. Arrancar `scripts/lab-vivo.ps1` al iniciar sesión (configuración persistente del PC).
5. Crear la tarea programada del generador, que gasta tokens de Claude: 2 pasadas al día para empezar (4 como mucho;
   15.1).
6. Hacia agosto de 2027, si se mueve el corte (4.2).
7. El cierre por tiempo antes de la ganancia mínima (6.5): aceptarlo propuesta a propuesta, como en «6 diarias», o
   pedir que el catálogo tenga salidas sin él.

**Riesgos y límites conocidos:**

| Riesgo o límite | Qué se hace |
|---|---|
| **Lo más probable es que no salga nada en semanas.** Con lo medido, ninguna señal acierta el sentido mejor que el azar. | El laboratorio lo enseña tal cual. Lo que falla también enseña (lecciones, `lo_que_no`). |
| **El fuera de muestra histórico está usado** (9.2) | Cuenta en N y la prueba en avance es obligatoria |
| **Monedas elegidas hoy** (SOL y XRP, por volumen y volatilidad): sesgo de supervivencia. Precios de Binance en USDT como aproximación a OKX EU en USDC, con menos libro en SOL y XRP. Funding de Binance, no de X-Perps. | Estrés de deslizamiento y de stops (B4). Comprobar las listas con las velas de OKX (`bot/velas_okx.py`) antes de proponerlas, en la fase 3. |
| **Otro flujo está editando `apertura.py` y `paper.py`** | Canario, firma del código y espera si no se pueden importar (7.6 y 11.7) |
| **Memoria justa en el PC** (2 GB libres el 29-10-2026) | Proceso único por defecto, caché compartida sin copias y guardas (11.6) |
| **Una familia con un fallo de futuro no detectado** | La prueba del recorte caza con certeza la mirada de 1 min o más en cada señal cortada (todas, o 60 por tramo) y, con los cortes al azar, a menudo la que quita señales (4.4). Lo que se escapa: una mirada que solo afecte a señales que no entran en la muestra, o que solo quite señales en momentos donde no cae ningún corte. Si se descubre después, se invalida su versión (11.8) y sigue contando en N. |
| **Cierre por tiempo antes de la ganancia mínima** | Todas las salidas del catálogo lo tienen: rompe «o gano más de 2 o pierdo 1» como la excepción de «6 diarias». Se mide, va en la tarjeta y en la propuesta, y aceptarla exige aceptar la excepción (6.5 y 13.6). |
| **El N podría volver a cero** si se borra o se cambia `lab_vivo.db` | `n_acumulado.jsonl` lo impide (9.1), salvo que se borre la carpeta entera |
| **El generador mira el fuera de muestra** | Solo recibe la etapa, el código y las cifras de dentro (13.5); el detalle no se lee (15.1) |
| **Las propuestas que no son del motor no se pueden operar sin código nuevo** | Lo dice la propuesta (10.4) y lo decide el usuario |

## 20. Implementación (29-10-2026)

**Lo que hay.** El paquete `bot/lab_vivo/` (fase 1 del apartado 18), `scripts/lab-vivo.ps1`, las pruebas
`tests/test_lab_vivo*.py` y la siembra. Solo archivos nuevos: no se ha tocado ningún archivo del bot ni de otros flujos.
El corredor **no está en marcha**: arrancarlo 24/7 (y al iniciar sesión) lo decide el usuario (19, puntos 3 y 4).

### 20.1 Módulos

| Módulo | Qué hace |
|---|---|
| `__main__.py` | Órdenes de 11.10 y `sembrar`. Fija los hilos de BLAS en 1 y los temporales en `lab_vivo\tmp` antes de importar numpy. `correr` admite además `--cpus` y `--memoria-mb` (los usa el vigilante). |
| `ajustes.py`, `criterios.py` | Variables `BOT_LAB_VIVO_*`, rutas y límites; criterios v1 con su fecha y su sha |
| `almacen.py`, `previas.py` | `lab_vivo.db` con los disparadores de solo añadir (pruebas, series y eventos) y la siembra de las 3.917 previas (con los `p_day` de los 2.000) |
| `datos.py` | Caché por época en `.npy` (mmap), `Mercado` con `recortado` y `ventana`, y velas sintéticas para las pruebas |
| `indicadores.py`, `filtros.py` | Indicadores causales y los 7 filtros comunes: régimen de BTC, volatilidad, funding, días, aperturas, régimen cripto de la skill y noticias (este, solo «no» hasta la fase 2) |
| `salidas.py`, `libro.py` | El catálogo de salidas y el libro de `medir_selectiva.py` portado (apartado 7) |
| `medidas.py`, `hipotesis.py`, `catalogo.py` | Estadística (8, 9 y 10.3), admisión sin datos (A1-A7 y A10) y conversión del catálogo del investigador |
| `etapas.py` | Admisión con datos (A8 y A9), rápida, completa, robustez y avance |
| `agenda.py`, `recursos.py`, `corredor.py` | Pausas, recursos de Windows (prioridad, afinidad, Job Object, memoria y CPU) y el supervisor |
| `salida_web.py`, `canario.py` | Las salidas del apartado 13 y el canario de 7.6 |
| `familias/` | `base.py`, el registro y 28 familias (20.3) |

### 20.2 Canario con datos reales (pasado el 29-10-2026)

`python -X utf8 -m bot.lab_vivo canario` reproduce `seis2_resumen.json` **exactamente**:
- dentro, 2.389 operaciones y −0,0569 R (−0,0535 con funding);
- fuera, 1.464 y −0,075 R (−0,0743 con funding);
- los mismos vetos que `medir_selectiva.py`: 10.292 por moneda ocupada, 799 por el tope de 6 del día y 40 por la pausa
  de pérdida del día.

Tarda ≈ 31 s con su caché (`lab_vivo\cache\canario-<firma>`). La caché se rehace en ≈ 70 s cuando cambian los archivos
de velas.

**Canario de comisiones** (pasado el 29-10-2026, tras la revisión): la tabla de `comisiones.py` es la esperada y 600
operaciones del canario (300 cortos y 300 con la mitad en 3R; 151 por el cerrojo, 172 por el stop, 149 por el
seguimiento y 128 por tiempo) cobran con el perfil real lo mismo que la fórmula independiente de `bruto_neto.py`
(diferencia máxima 1,8e-15 %).

**Caché de la época «2026-10»** (`lab_vivo\cache\2026-10-<firma>`, 1,1 GB, ≈ 4 min): hecha el 29-10-2026 con
`preparar-datos`, del 28-07-2022 (calentamiento) al 16-10-2026.

**Admisión con datos de las 41 hipótesis** (`validar`, que no mira ningún resultado):
- A8, la prueba del recorte con datos reales, la pasan las 28 familias. Ojo: aquella prueba no veía una mirada de 1
  min al futuro (20.8). Con la prueba nueva (un corte en cada señal y cortes al azar con t ≤ T) se volvieron a pasar las
  **216 variantes** con datos reales el 29-10-2026: 12.369 cortes en señales y 4.320 al azar, **ninguna cambia**. Las 6
  variantes con el régimen cripto, que con el calentamiento de antes no daban ninguna señal en los tramos, ahora sí.
- A9 (≥ 0,25 señales por día natural dentro de muestra) deja fuera 64 variantes. Caen enteras H13, H16, H26 y H27, como
  ya anticipaban sus frecuencias en el catálogo. El corredor rechaza variante a variante y solo descarta la hipótesis
  si no le queda ninguna.

### 20.3 Familias (todas pasan la conformidad de 6.4)

| Familia | Hipótesis |
|---|---|
| `apertura_impulso` | La referencia (SEIS2) y el canario |
| `ruptura_canal` | Siembra, H03 y H29a |
| `compresion_ruptura`, `tendencia_diaria` | Siembra |
| `momento_serie` | H01 (diario, modo «estado»), H02 (4 h, modo «flanco») y H29b |
| `donchian_conjunto` · `precio_media` · `ruptura_volatilidad` · `keltner` · `squeeze_bollinger` | H04 · H05 · H08 y H29c · H06 · H07 |
| `rango_horario` | H09 (Asia) y H29e, H23 (fin de semana) y H25 (Deribit) |
| `fuerza_relativa` | H10 (momento) y H11 (reversión) |
| `funding_extremo` · `funding_ruptura` · `funding_hora` | H12 · H13 · H14 |
| `extremo_1h` | H15 (cascada) y H29d, H16 (euforia) |
| `sesion_extrema` · `dia_anormal` · `lider_btc` · `cociente_alt_btc` | H17 · H18 · H19 · H20 |
| `calendario` | H22 (hora), H26 (cambio de mes) y H28 (previa macro) |
| `fin_de_semana_nyse` · `reaccion_macro` · `flujo_tomador` · `volumen_vela` · `rsi2_tendencia` | H24 · H27 · H30 · H31 · H32 |
| `media_hora` · `cierre_nyse` | H36 (informativa) · H38 |

**Pendientes** (`lab_vivo\cola\esperando_datos\pendientes.json`):
- H21 y H37: dos patas (pares y carry). El libro del paper opera una pata por operación.
- H33 a H35: noticias en vivo. Esperan la familia `noticias_vivas` de la fase 2.
- H25-V6: mide el «clavado», sin operación.

La siembra de 6.7 y el catálogo convertido suman **41 hipótesis y 216 variantes** (197 del catálogo). Todas pasan
A1-A7 y A10. Hay 10 variantes marcadas fuera de reglas (H14-V6, H22-V1, H26-V1, H28-V1 y las 6 de H36).

### 20.4 En qué se aparta del diseño (decidido antes de medir nada)

- **Catálogo de salidas ampliado** con lo que prerregistró el investigador:
  - A con `max_hold_h` {24, 48, 72, 96, 120, 168}; C con {24, 72, 96, 168, 336}; H con {24, 48, 72, 96, 120, 168}.
  - **OBJ**: horquilla con el objetivo de la señal (H24). **INFO**: sin stop ni objetivo, con cierre por tiempo; solo
    fuera de reglas.
  - `sl_max` ∈ {5, 10}: el catálogo usa el 10 % que prerregistró.
- **El suelo del cerrojo es el F** (≥ 2R y ≥ 3 % neto por operación, con el coste del instrumento y el funding esperado
  hasta el tope activado), en vez del «2R con s ≥ 2 % y 2,5R con s < 2 %» del borrador. Deja ≥ 3 % neto con el perfil
  en uso y se recalcula en el estrés sin X-Perps.
- **Las señales llevan tres campos más:**
  - `prioridad`: si en el mismo minuto no caben todas;
  - `objetivo_r`: solo para OBJ;
  - `fin_ms`: cierre por tiempo propio de la señal (H18 a las 00:00 UTC de d + 2 y las informativas). Solo acota la
    parte sin activar: el cerrojo activado sigue su tope.
- **Una decisión por moneda y minuto:** `Familia.senales` se queda con la de más prioridad.
- **La etapa rápida usa `Mercado.recortado(corte)`:** no ve ni una vela del fuera de muestra. Las operaciones que
  siguen abiertas en el corte son «fin de datos» y no cuentan.
- **Barras:** se agregan desde la rejilla densa, alineadas a UTC, y solo las cerradas (`t_cierre ≤ hasta`).
- **Época actual, «2026-10»:**
  - periodo del 01-10-2023 al 30-09-2026;
  - 430 días de calentamiento, solo para indicadores (el régimen cripto de la skill pide 420 cierres diarios);
  - 15 días de cola, para que acaben las operaciones de los últimos días.
- **La admisión con datos tiene 20 min de tope** (no 10): la de `apertura_impulso` pasa dos veces por 15.000 aperturas.
- **La consolidación corre en el corredor,** no en un subproceso: escribe en `lab_vivo.db`, y solo escribe él.
- **H29 se parte en 5 hipótesis,** una por madre, con el filtro común `regimen_cripto` (`no_en_contra` o
  `a_favor_estricto`).
- **H38 decide 15 min después del cierre de NYSE** (la regla de los cierres de bolsa del laboratorio), no 5.
- **Código del bot no importable:** la tarea sale con el código 76 y el corredor espera 10 min sin gastar intentos.
- **Windows:** `aux.json` es un nombre reservado (AUX), así que el archivo de la caché se llama `extra.json`.
- **Sin hacer todavía:**
  - volver a medir las candidatas al cambiar de época (hoy conservan su medida);
  - el N efectivo conjunto con las series de los 2.000 (se suman 381, como dice 9.4).

### 20.5 Cómo se arranca (lo decide el usuario)

1. `M:\BotFundamental-runtime\venv\Scripts\python.exe -X utf8 -m bot.lab_vivo sembrar`. Hecho el 29-10-2026: las 41
   hipótesis están en `cola\entrada`.
2. Arrancar (PowerShell): `Remove-Item "M:\BotFundamental-datos\lab_vivo\parar" -ErrorAction SilentlyContinue;
   powershell -NoProfile -ExecutionPolicy Bypass -File "M:\OneDrive\Bot de trading fundamental\scripts\lab-vivo.ps1"
   -Suelto`. Arranca el vigilante suelto.
   - El vigilante arranca el corredor con `pythonw` (Win32_Process.Create) y lo relanza si cae o se cuelga.
   - Con `-Prueba` dice qué haría.
   - **Parar:** `New-Item -ItemType File -Force "M:\BotFundamental-datos\lab_vivo\parar"`. El corredor guarda el punto
     de sus tareas y sale; el vigilante lo espera hasta 2 min y termina. El archivo `parar` se queda: hay que borrarlo
     para volver a arrancar (el primer comando lo hace).
   - **Pausar sin parar:** el archivo `pausa_manual` (no empieza nada nuevo); o el botón de la pestaña cuando esté
     cableada.
   - **Ver cómo va:** `M:\BotFundamental-runtime\venv\Scripts\python.exe -X utf8 -m bot.lab_vivo estado`.
3. Lo primero que hace el corredor:
   - la caché de la época (≈ 5 min la primera vez);
   - el canario;
   - la admisión con datos.

   Después, la etapa rápida de 24 hipótesis al día, siempre fuera de las pausas de las aperturas.

### 20.6 Pruebas

| Archivo | Qué prueba |
|---|---|
| `test_lab_vivo_almacen.py` | Esquema, disparadores de solo añadir, N una vez por variante y previas |
| `test_lab_vivo_hipotesis.py` | Formato, firma, duplicadas, reglas, plan imposible, fuera de reglas, filtros, buzón atómico y catálogo |
| `test_lab_vivo_libro.py` | simulate_plan a mano, una posición por moneda, topes del día, pérdida del día, exposición y 4 posiciones, funding, comisiones, sin X-Perps, stops, fin de datos, hueco, estreses y cifras fijas |
| `test_lab_vivo_familias.py` | Conformidad de las 28 familias: esquema, recorte (con corte en cada señal), determinismo, flanco, velocidad, sin red ni disco |
| `test_lab_vivo_familias_casos.py` | Señales sabidas a mano de las familias principales |
| `test_lab_vivo_medidas.py` | HAC, DSR, BH, N efectivo, bases emparejadas, sin mayores, sin mejor semana, avance trimestral, meta, Holm y cuenta honesta |
| `test_lab_vivo_corredor.py` | Pausas y agenda, prioridades, turno y cupos, reanudación, ingesta idempotente, fallos, memoria, CPU, mandos, canario fallido, familia cambiada, código no importable y lock |
| `test_lab_vivo_salida_web.py` | El flujo entero en el proceso y las salidas del apartado 13: estado, diario, tarjeta, generador (sin el fuera de muestra), propuestas (retiradas, solo la última, la excepción del tiempo), decisiones, avance (sin fantasmas) y revocación |
| `test_lab_vivo_recorte.py` | La prueba del recorte nueva caza familias trampa que miran 1 min, 15 min y 1 h al futuro (todas las señales cortadas), la que quita señales y nada en una causal; la de antes no veía 1 min; A8 de verdad rechaza la trampa |
| `test_lab_vivo_vigilante.py` | El lock con la hora de creación: el corredor reemplaza un lock con el PID reutilizado; el vigilante (`-Prueba`) no mata un PID reutilizado y sí reconoce al corredor |
| `test_lab_vivo_revision.py` | El N fuera de la base y la herencia; el suelo del N efectivo y su aviso; sin tarjetas en las pausas; el canario de comisiones (y que caza un libro que cobra mal y otra tabla); cierres por tiempo; las órdenes con prioridad baja, pausas, `consolidar` con el corredor en marcha y `heredar-n` |

Ninguna usa la red ni escribe fuera de su carpeta temporal en M:. El canario con datos reales no va en la batería
(20.2).

### 20.7 Primera tanda (29-10-2026, 15:48-16:33)

`correr --una-pasada` sobre el catálogo inicial, lanzado suelto con `lab_vivo\tmp\lanzar_primera_tanda.py`. El 24/7
**sigue sin arrancar**. El informe entero está en `M:\BotFundamental-datos\lab_vivo\primera_tanda.md`.

**Cómo se corrió:**
- **Cupo.** Lo único distinto fue el cupo del día de ese proceso: 41 hipótesis (`BOT_LAB_VIVO_CUPO_DIA`), el catálogo
  entero. No cambia el N. El cupo por defecto sigue en 24.
- **Recursos.** Un proceso de cálculo (no había 4 GB libres), BELOW_NORMAL, núcleos 3, 4 y 5 y Job Object de 2.500 MB.
- **Sin pausas por medio.** Ninguna apertura cayó entre las 15:48 y las 16:33.
- **Canario.** Pasó con la firma `2e9e28c1179c`, que no cambió durante la pasada.
- **Trabajos.** 121, sin fallos. Duraron 44 min: no hizo falta la pasada reducida.

**Resultado: ninguna candidata.**
- **Embudo.** De 41 hipótesis y 216 variantes:
  - la admisión deja 152 variantes (64 dan muy pocas señales);
  - pasan la rápida 85 de las 144 dentro de reglas;
  - pasan la completa 13;
  - no pasa la robustez ninguna;
  - a la prueba en avance no va nada.
- **N.** Se han medido 151 variantes (96 con fuera de muestra; N efectivo 57). Benjamini-Hochberg sobre m = 4.068: la q
  más baja es 1,0. Sharpe deflactado con N = 2.355: el más alto es 0,037.
- **Sin corregir.** Ninguna de las 89 dentro de reglas con fuera de muestra tiene p < 0,05.
- **Dentro y fuera.** Las 85 que pasan la rápida dan +0,101 R por operación dentro y −0,004 fuera. Dentro fue un mercado
  muy alcista (BTC +335 %) y fuera bajista (BTC −29 %).
- **Una sola semana.** Del 17 al 24-08-2026 (BTC +24 %, XRP +54 %) es la mejor semana de 11 de las 13 que llegaron a la
  robustez. Sin ella pierden 9.

**Las más cercanas:**
- **`h06-keltner_1h:V3`.**
  - Pasa B1-B6 y cae solo en B7 y B8.
  - Fuera da +0,128 R por operación: +0,102 R al día, que es +0,051 % de la cuenta. Hace 0,80 operaciones al día y su p
    es 0,16.
  - Sin X-Perps da −0,154 R.
- **`tendencia_diaria-siembra:V1`.**
  - Es la de más R al día fuera: +0,106 R al día, que es +0,053 % de la cuenta. Le faltan 1,89 R al día (0,95 %).
  - Cae en K4, y sin sus 5 mayores operaciones pierde.

**Otras cifras:**
- **Cuenta honesta (9.6).** Da +0,063 R por operación, con 2 trimestres de 5 en negativo.
- **Referencia.** «6 diarias v2» con comisiones reales da −0,0345 R dentro de muestra, frente a −0,0569 con las del
  backtest.

### 20.8 Revisión adversarial y arreglos (29-10-2026)

Una revisión adversarial de la tarde del 29-10-2026 rehízo a mano las dos variantes más cercanas (señales desde las
velas en bruto, 1.408 de 1.408 y 426 de 426; 120 operaciones resimuladas, diferencia 0,0000 R; la corrección global
cuadra) y comprobó con carga real que el corredor no frena al bot (GET /health, 26-27 ms de mediana con 1 y 2 procesos
de carga). Encontró 2 fallos graves y 9 medios. Arreglos (solo en los archivos de este flujo; los criterios v1 no
cambian, ni su versión ni su sha):

| Hallazgo | Arreglo | Dónde |
|---|---|---|
| **Grave.** El vigilante daba por vivo cualquier proceso con el PID de un `corredor.lock` viejo y, si el latido era viejo, lo cerraba con `taskkill /T /F`: tras un reinicio podía ser el bot o llama-server | El lock lleva la hora de creación del proceso y la orden. El vigilante solo toca un PID cuya línea de órdenes lleva `bot.lab_vivo correr` y cuya `CreationDate` coincide; el corredor reemplaza un lock viejo | `recursos.inicio_proceso` y `mismo_proceso`, `corredor.tomar_lock`, `scripts/lab-vivo.ps1`, `test_lab_vivo_vigilante.py` |
| **Grave.** La prueba del recorte no veía una mirada de 1 min al futuro (0 de 10 trampas) | Un corte en cada señal (todas, o 60 por tramo) y cortes al azar con t ≤ T; el calentamiento incluye el de los filtros (las variantes con el régimen cripto no daban señales en los tramos). Las 216 variantes con datos reales: ninguna cambia (20.2) | `recorte.py`, `etapas.admision`, `test_lab_vivo_recorte.py`, `test_lab_vivo_familias.py` |
| El N efectivo de Li y Ji satura (≈ 620 con miles de series de 411 días) | Suelo: la suma por hipótesis; aviso cerca de la saturación. Hoy: Li y Ji 57, por hipótesis 77, N_dsr 2.375 (antes 2.355), z 3,493 | `corredor.estadistica_global`, `medidas.n_efectivo_bloques` (9.4) |
| El N volvía a cero si se borraba o se cambiaba `lab_vivo.db` | `n_acumulado.jsonl`, de solo añadir; con la base por debajo, no se mide hasta `heredar-n` (9.1) | `registro_n.py`, `__main__.py` |
| El generador veía el detalle del fuera de muestra | Solo la etapa, el código y las cifras de dentro; lista de lo que no lee; el texto de la tarea, nuevo (13.5 y 15.5) | `salida_web.escribir_generador`, `para_el_generador_estado.json` |
| Una propuesta revocada seguía pendiente y se podía aceptar | Se retira al revocarla, al descartarla en avance o al proponerla otra vez; solo vale la última; id por propuesta; una decisión anterior a la propuesta no vale (13.6) | `salida_web.retirar_propuestas`, `corredor.leer_decisiones` |
| Un tercio de las operaciones cierra por tiempo entre −1R y +2R, y el documento decía «todas cumplen las reglas» | Se mide, va en la tarjeta y en la propuesta, y aceptarla exige aceptar la excepción de «6 diarias» (6.5) | `medidas.cierres_por_tiempo`, `salida_web.excepcion_tiempo` |
| El canario no cubría las comisiones reales | Canario de comisiones: la tabla y la fórmula independiente de `bruto_neto.py` sobre las operaciones del canario (7.6). Con datos reales, el libro reproduce `seis2_resumen.json` y 600 operaciones del canario (300 cortos, 300 con la mitad en 3R; cerrojo, stop, seguimiento y tiempo) cobran lo mismo que la fórmula (diferencia máxima 2e-15 %) | `canario.py` |
| `validar` y `consolidar` corrían sin prioridad baja ni pausas (y `consolidar` escribía en la base con el corredor en marcha) | Prioridad baja y núcleos del laboratorio; no empiezan en una pausa; con el corredor en marcha, `consolidar` se la pide a él (11.10) | `__main__.py` |
| El supervisor recalculaba todas las tarjetas cada 30 s, también en las pausas | Solo al cambiar algo o cada 10 min, nunca en una pausa; una sola pasada de `mejores` (11.1) | `corredor.escribir_salidas`, `salida_web.pesado` |
| La tabla de avance podía acumular operaciones fantasma | Cada recálculo sustituye las filas de la variante; un resultado viejo no cuenta; cada variante con su ventana de datos (8.5) | `corredor._ingerir_avance`, `etapas.avance` |

**Lo que no cambia:** los criterios v1, el catálogo de salidas y las cifras de la primera tanda (20.7): con el N nuevo
la q más baja sigue en 1,0 y el Sharpe deflactado más alto baja de 0,0369 a 0,0367. La firma del código cambia, así que
el corredor volverá a pasar los dos canarios antes de medir nada.

## 21. Noticias de 3 años (29-10-2026)

**Para qué.** El usuario preguntó el 29-10-2026 si el laboratorio «tiene en cuenta las noticias». Hasta hoy no podía:
solo había precio, volumen y funding de 3 años. Las noticias del bot empiezan el 23-10-2026 y los titulares públicos
bajados, el 29-07-2026 (`noticias_historia`, solo exploratoria). Este apartado añade un histórico de noticias de 3 años
con su hora, la clave de datos `noticias_3a` y dos familias que la usan. Lo programó la sesión de noticias; no cambia
los criterios v1, los cupos ni el catálogo de salidas.

### 21.1 Fuentes (cómo se eligieron)

Antes de elegir se miró cómo se consiguieron los titulares de `controles/fuentes/` y `controles/bruscos/`:
- **91 días** (`controles/historia_noticias_descarga.py` y `historia_noticias_gdelt.py`): la API REST de WordPress de 24
  medios de cripto (hora de publicación al segundo), los mapas del sitio de CoinDesk y Cointelegraph (solo `lastmod`)
  y la lista de artículos de GDELT DOC 2.0, que solo cubre los últimos 3 meses.
- **54 bruscos** (`controles/bruscos/descarga_bruscos.py`): la misma API de WordPress de los 24 medios, en las ventanas
  de 3 años alrededor de cada episodio. Funcionó en los 24.

Con eso, el histórico junta dos fuentes públicas, sin clave ni cuenta:

| Fuente | Qué da | Medido con 1 día de muestra (20-10-2026) |
|---|---|---|
| GDELT 2.0 GKG en inglés (`data.gdeltproject.org/gdeltv2/<AAAAMMDDHHMMSS>.gkg.csv.zip`, uno cada 15 min) | Dominio, URL, temas, tono y titular (`<PAGE_TITLE>` de Extras, en el 99,8 % de las filas) | 96 archivos, 488 MB (1.517 MB descomprimidos), 34 s de bajada con 4 hilos; 113.378 artículos, de los que el filtro del bot deja 248 de cripto y 4.588 macro (2 MB guardados) |
| API REST de WordPress de los 24 medios de cripto | Titular y hora de publicación al segundo (`date_gmt`) | bitcoinist.com, septiembre de 2026: 191 artículos en 3 peticiones (9 s); 107 de cripto |

- **Tamaño de GDELT.** Del 01-07-2023 al 28-10-2026 son 115.132 archivos y 620 GB comprimidos (la lista maestra). En
  junio de 2025 faltan unos 17 días: GDELT no los tiene. Se bajan en memoria, se comprueba el md5 de cada uno y solo se
  guarda lo filtrado (≈ 2 MB al día, unos 2,5 GB en total). Con un tope de 10 MB/s (la wifi la usa también el bot) y 2
  procesos en prioridad de reposo, ≈ 17 h.
- **Por qué los dos.** GDELT ve ≈ 250 titulares de cripto al día, casi todos de prensa general; los 24 medios publican
  cientos. GDELT aporta la macro y la prensa general; los medios, la cripto.
- **Sello (cuándo se pudo conocer).** GDELT publica cada archivo unos 10 min **antes** de la hora de su nombre
  (`Last-Modified` de seis archivos de 2023 a 2026): el sello es la hora del nombre, o su `Last-Modified` si es
  posterior. En WordPress, `date_gmt`.

### 21.2 Filtro, historias y selección

- **Filtro:** el del bot en vivo, `filters.classify` con idioma inglés. Solo se guardan las clases «cripto» y «macro», las
  que en vivo van a la IA local. `noticias_historia.puerta` usa las mismas expresiones y máscaras que `classify` para
  esas dos clases y evita llamarla en las filas que no lo son. En 22.090 titulares de 12 archivos de 2024 no pierde
  ninguno de los 1.120 que `classify` da como cripto o macro. Las fuentes llevan prefijo (`gdelt:`, `wp:`): `classify`
  trata como oficiales las fuentes cuyo id empieza por fed, bls, ecb o sec (fedscoop.com no es la Fed).
- **Medios:** la mesa del histórico solo ve los medios que sigue el bot (los dominios de `bot/fuentes/*.json`, sin
  Google News ni relays) y los 24 de cripto, como la de vivo. La lista se congeló el 29-10-2026 en
  `noticias_historia/medios_vivos.json` (2.231 medios). 13 de los 24 medios de cripto no son feeds del bot en vivo: se
  dejan porque son la cripto del histórico (y los del estudio de 91 días).
- **Historias:** `noticias_historia.Mesa` es la deduplicación de `desk.NewsDesk` (`filters.tokens` y `filters.similar`
  con la primera pieza de cada historia de las últimas `config.DEDUPE_WINDOW_S`, 3 h). Las demás piezas la confirman:
  medios distintos con su sello.
- **Selección para la IA local (fijada el 29-10-2026 solo por volumen, antes de mirar ningún precio):** una historia va
  al triaje si es un **racimo**: ≥ 2 medios distintos en los 30 min desde su primera publicación si es de cripto, y ≥ 4
  si es macro (`RACIMO_N`). En octubre de 2026 salen 3,4 de cripto y 3,5 macro al día (con ≥ 3 macro serían 5,8): unas
  8.400 en 3 años.
  - Por qué hace falta seleccionar: la IA local tarda ≈ 7 s por titular y el triaje en vivo la tiene ocupada del 80 al
    100 % casi todas las horas (400-730 titulares por hora del 24 al 29-10-2026; el 29-10 a las 22:40, 111 de 112
    sondeos de su hueco, ocupado). Triar todo (≈ 300 historias de cripto y 600 macro al día) llevaría años de huecos.
  - Lo que se ve en el volumen y queda dicho: muchos racimos de cripto son notas de prensa pagadas que publican a la
    vez varios medios (Travala, Bybit, MEXC…), y los de macro, partes de guerra y terrorismo; la prensa local del Reino
    Unido (Newsquest, Reach) y las cadenas locales de NBC repiten el mismo texto. En vivo pasa igual: es la misma mesa.
- **Desde cuándo se puede usar** cada historia en racimo: el sello del medio que lo completa más 1 min (en vivo el
  triaje tarda ≈ 7 s), al minuto siguiente.

### 21.3 Triaje con la IA local (`python -m bot.noticias_historia triaje`)

- **La misma petición que en vivo:** `LocalAI.classify` tal cual (`local_ai.SYSTEM`, el esquema estricto, temperatura 0,
  700 tokens, sin razonamiento), el titular de la primera pieza, la fuente con el nombre y el nivel de su feed en vivo
  (sin feed: el dominio y T2) y las monedas que detectan las reglas; la respuesta pasa por `normalize_triage` y la bolsa
  de la región, como `LocalAI._worker`. La prueba compara la petición con la de `LocalAI.classify`. **A ciegas del
  precio:** el modelo solo ve el titular, la fuente y las monedas.
- **Sin quitarle el turno al triaje en vivo:** una petición cada vez, solo cuando el hueco del servidor (`/slots`) lleva
  3 s libre (el bot no tiene nada esperando) y no hay `gpu.lock` de la fábrica. Si el bot manda algo mientras, espera
  como mucho una petición (≈ 7 s). Si el servidor está apagado, no lo arranca.
  - **Problema medido el 29-10-2026:** a las 23:55, 1 de 123 sondeos de 0,5 s encontró el hueco libre. Con solo los
    huecos, las ≈ 8.400 historias tardarían semanas.
  - **`--cuota q`** (de `triaje` y `seguir`; 0 por defecto, hasta 0,5; **lo decide el usuario**): pide sin esperar al
    hueco (la petición espera en la cola del servidor detrás de la que esté en curso) y luego descansa lo justo para no
    usar más de esa fracción del tiempo. Con 0,2, unas 2.400 historias al día (≈ 3,5 días); el triaje en vivo, con su
    cola por prioridad, deja sin triar lo menos prioritario de su cola en esa misma proporción.
  - **En marcha con `--cuota 0.2` desde el 30-10-2026 a las 00:35**, aprobada por la sesión principal: ninguna operación
    del paper depende de lo menos prioritario de la cola (`seis4r3` solo usa las noticias como veto de grupos fiables y
    ninguno pasa la puerta). Las T0 y T1 frescas van primero en la cola del bot y esperan como mucho una petición del
    histórico (≈ 7 s); la importancia solo se sabe después del triaje, así que lo que se protege es lo que la cola pone
    delante.
  - **Vuelta sola a los huecos** (`vigilar_vivo` y `vivo_aguanta`, cada 10 min, solo lectura de `bot.db` y `bot.log`):
    60 min solo en los huecos si el p90 del retraso de captura a triaje de las T0 y T1 frescas de cripto y macro pasa de
    300 s, si 3 o más llevan 15 min sin triar, si la cola llena tira más de 60 noticias en 30 min o si no se puede leer.
    La última medida queda en `noticias_historia\vigilancia_vivo.json` (al empezar: p90 de 34 s en 25 noticias y
    ninguna tirada).
  - **Cómo quitarla:** crear `noticias_historia\parar_triaje` (para `seguir` en segundos), borrarlo y relanzar `seguir`
    sin `--cuota` desde su copia del código (`noticias_historia\codigo\seguir-20261030b`): `python -X utf8 -B -m
    bot.noticias_historia seguir --esperar 15`.
- **Orden** (solo cuándo, no qué): los 91 días de `controles/fuentes/`, las ventanas «antes» y de control de
  `controles/bruscos/` y luego el resto, de lo más nuevo a lo más viejo. `triaje/AAAA-MM.jsonl`, una línea por
  historia; se puede relanzar y no repite nada. El archivo `parar_triaje` lo para.
- **Ojo (sesgo que no se puede quitar):** Qwen3.5 9B se entrenó con noticias anteriores a su fecha de corte. Para un
  titular de 2023-2025 puede «saber» qué pasó después aunque el mensaje no traiga ningún precio. Si el acierto del
  histórico sale muy por encima del 49 % medido en vivo en los años que el modelo conoce, es eso, no ventaja: el
  informe lo da por año (21.7).

### 21.4 Lo que lee el laboratorio

- `python -m bot.noticias_historia lab` compila `noticias_historia/lab/noticias.npz` y `meta.json` **solo cuando todas
  las historias seleccionadas están triadas** (si no, el laboratorio mediría otra cosa cada vez). Lleva su firma (sha
  del contenido y de la selección).
- `bot/lab_vivo/noticias.py`: `Noticias` con `t` (desde cuándo se puede usar), `t0`, `n30` (medios en 30 min), `nmed`,
  `clase`, `mon` (bits de las monedas del triaje; 0 = ninguna), `dir`, `imp`, `tipo`, `cred`, `nov`, `mag`, `fuente` y
  `medio`; `tramo`, `sentido` (±1 con |dir| > 15, como `local_ai.sentiment_of`), `de_moneda` y `cobertura`.
- `datos.py`: la caché de la época guarda las noticias del periodo (`<caché>\noticias\`), `Mercado.noticias` da las de
  `desde ≤ t < hasta` y `recortado` las corta como a las velas. **Con noticias compiladas cambia la firma de la época:**
  la caché se rehace y se repite el canario. Sin el archivo, nada cambia (ni la firma).
- `fuente_sintetica(noticias=True)`: noticias al azar para la conformidad de 6.4, que ahora las lleva.

### 21.5 Familias (versión 1; las dos pasan la conformidad y sus pruebas)

| Familia | Momento | Señal | Parámetros |
|---|---|---|---|
| `noticia_racimo` | continuo | Una historia en racimo de ≥ n medios con sentido (o sin mirarlo) que el precio confirma: «rango_4h» (H34: la primera barra de 15 min que cierra fuera del rango de las 4 h previas, en las 2 h siguientes; stop = máx(1,5 %, otro extremo + 0,1 %)) o «reflejo» (`entrada_noticias.confirmar`: el primer cierre de 1 min en 60 min a favor ≥ máx(0,5 %, 2σ de 60 min de las 24 h previas)). Por moneda y sentido, otra confirmación a menos de 30 min no sale. | `n` {2, 3, 5}; `sentido` {triaje, ruptura}; `modo` {favor, contra}; `confirmacion` {rango_4h, reflejo}; `generales` {no, todas}; filtros `importancia_min` {0, 5, 7} y `grupo` {todos, fiable}; `regimen_btc` y `volatilidad` |
| `noticia_apertura` | aperturas | En cada apertura de `LAB_WINDOWS`, el signo de la suma de los sentidos de las historias de las `horas` previas (de esa moneda y, con generales, las que no nombran ninguna); si el movimiento de la apertura al cierre de `medida_min` va a favor ≥ `min_mov_pct`, entra a favor (o a la contra) | `windows`; `horas` {4, 8, 12}; `n` {2, 3, 5}; `medida_min` {15, 30, 60}; `min_mov_pct` {0; 0,2; 0,5}; `modo`; `generales`; filtros `importancia_min` y `grupo`; `regimen_btc` y `volatilidad` |

- **Unidad:** historia×moneda, la de `bot/fiables.py`. Solo cuentan las historias con `t ≤` la decisión.
- **Grupo «fiable»:** los grupos tipo×sentido de `bot/fiables.py` con sus criterios fijados (n ≥ 30, ≥ 3 días con ≥ 5,
  acierto ajustado a 4 h ≥ 60 %, Wilson al 90 % ≥ 50 % y ≥ 50 % en cada semana UTC con ≥ 5), con los 30 días UTC
  completos anteriores (`base.grupos_fiables` y `base.fiables_por_dia`). Un día cuenta cuando han pasado sus 4 h de
  medida y 2 h de margen, como en vivo. Sin la puerta para operar: es una variante, no un requisito.
- **Ayudantes en `familias/base.py`** (sección «noticias»): `rango_previo`, `sigma60_pct`, `unidades_noticias`,
  `fiables_por_dia`, `grupos_fiables` y `ultimo_dia_completo`. Si cambian, hay que subir la versión de las dos familias.
- **A6 con datos** (`etapas.admision`): una familia con `noticias_3a` no se mide si las noticias cubren menos del 50 %
  de los días del periodo (`criterios` `cobertura_min`), o si no hay archivo compilado.
- **Pruebas:** `tests/test_lab_vivo_familia_noticia_racimo.py`, `tests/test_lab_vivo_familia_noticia_apertura.py`,
  `tests/test_lab_vivo_noticias.py` y `tests/test_noticias_historia.py` (velas y noticias hechas a mano, la señal sabida,
  un corte en cada señal con `recorte.comprobar`, sin red).

### 21.6 Hipótesis (prerregistradas el 29-10-2026 a las 23:40, antes de medir nada del precio con estas noticias)

En `lab_vivo\cola\esperando_datos\noticias\`, autor «sesion-noticias» (añadido a `hipotesis.AUTORES`), con las reglas del
usuario y una sola cosa cambiada por variante:

| Id | Familia | Variantes |
|---|---|---|
| `h-20261029-2340-noticia_racimo-h34` | `noticia_racimo` | H34 del catálogo: V1 n 3 y rango de 4 h, salida A con stop estructural (≤ 10 %); V2 n 5; V3 horquilla 3R a 24 h; V4 sin mirar el sentido (el control del triaje); V5 a la contra (stop fijo de 2 %); V6 importancia ≥ 7. Todas con las generales para las cuatro monedas. |
| `h-20261029-2340-noticia_racimo-reflejo` | `noticia_racimo` | H33 y H35: la regla de `entrada_noticias` (reflejo en 60 min y su horquilla de 1,5 %, 3R y 24 h); V2 grupo fiable; V3 salida A con 2 %; V4 a la contra; V5 solo BTC y ETH; V6 importancia ≥ 7 |
| `h-20261029-2340-noticia_apertura-impulso` | `noticia_apertura` | La operativa del usuario: 8 h de noticias, impulso de 15 min ≥ 0,2 %, salida A con 2 %; V2 30 min; V3 4 h; V4 grupo fiable; V5 a la contra; V6 importancia ≥ 7 |

Pasan la admisión sin datos (A1-A7 y A10). Esperan ahí, y no en `cola\entrada\`, por tres cosas (`LEEME.md` de la
carpeta): el corredor en marcha usa el código de la rama main, que aún no tiene las familias ni el autor; el archivo de
noticias se compila cuando acabe el triaje; y `validar` tiene que dejar de decir NO VALE con los datos reales (A6, A8 y
A9). Entonces se copian a `cola\entrada\` sin tocarlas. Todo lo que mida el corredor contará en el N acumulado.

### 21.7 Informe de calidad (`python -m bot.noticias_informe`)

`noticias_historia/informe.json` e `informe.md`: descarga, cobertura por mes, medio y moneda, retraso de publicación,
retraso frente al precio (si el sentido coincide con las 12 h previas o con las 4 h siguientes), acierto por año y
clase, y si repite lo medido: los 91 días (acierto a 4 h ajustado frente al 48,0 % de Claude y el 49,2 % de los
titulares públicos; cuántos de los 48.272 titulares de aquel estudio están en el histórico) y los 54 bruscos (consenso
de las 12 h antes frente a 20 de 43, 46,5 %). Lo que necesita el triaje sale «pendiente» hasta que lo haya.

### 21.8 Cómo se sigue (`python -m bot.noticias_historia seguir`, solo)

1. Termina la descarga de GDELT (`gdelt`, que se puede relanzar; el archivo «parar» la para).
2. Rehace todas las historias en una sola pasada cronológica (`historias`).
3. Tría en los huecos de la GPU hasta acabar.
4. Compila el archivo del laboratorio (`lab`) y escribe el informe.
5. El usuario decide si se sube el código a main; después, las hipótesis pasan a `cola\entrada\` (21.6).

### 21.9 Las noticias para reforzar lo que ya va mejor (30-10-2026)

Lo pidió el usuario el 30-10-2026 («que el agente siga buscando y utilice las noticias para fortalecer las
estrategias»): no solo familias de noticias sueltas, sino las noticias como filtro de las entradas que ya van mejor.

- **Filtro común `noticias_previas`** (`filtros.py`, 6.3): cualquier familia lo puede declarar. Usa solo las historias
  que se podían usar antes de la decisión (`filtros.suma_noticias`: `t` en [decisión − horas, decisión)).
- **Familia `impulso_noticias` v1**: las entradas de `apertura_impulso` tal cual (las hereda) con ese filtro, y la clave
  `noticias_3a`. Para que un cambio de `apertura_impulso` no cambie en silencio sus resultados, fija su versión y la
  firma de su archivo (sin retornos de carro): si cambian, no se carga hasta que se suba su versión. Pruebas en
  `tests/test_lab_vivo_familia_impulso_noticias.py` (la suma de noticias a mano, las entradas idénticas sin filtro y
  un subconjunto con él, y el recorte en cada señal).
- **Hipótesis prerregistradas el 30-10-2026 a las 00:10** (en `cola\esperando_datos
oticias\` con las otras tres y
  las mismas condiciones para pasar a la cola; 14 variantes que contarán en el N):

| Id | Base | Variantes |
|---|---|---|
| `h-20261030-0010-impulso_noticias-seis4r3` | La de la cartera paper desde el 29-10-2026 (`seis4r3`: entradas de «6 diarias» y horquilla de stop 3 %, 4R y 7 días) | V1 no en contra (8 h); V2 solo a favor; V3 4 h; V4 12 h; V5 importancia ≥ 7; V6 sin las generales |
| `h-20261030-0010-impulso_noticias-ranking1` | La 1.ª del ranking el 30-10-2026 (las mismas entradas con stop 2 %, 4R y 7 días) | V1 no en contra; V2 solo a favor; V3 4 h; V4 importancia ≥ 7 |
| `h-20261030-0010-impulso_noticias-ranking5` | La 5.ª (laboratorio de 2.000: impulso de 30 min ≥ 0,2 % con volumen relativo 2, entrada a +60 min, stop 2,5 %, 3R y 7 días) | las mismas cuatro |

- No entran: la 2.ª del ranking (objetivo 10R, fuera del catálogo de salidas), la 3.ª (E2, descartada por el usuario) ni
  la 4.ª (las mismas entradas que la 1.ª con 3R).
- **Ojo:** estas bases se eligieron por su resultado (la de la cartera y las primeras del ranking). Su fuera de muestra
  ya está usado y lo que diga sobre la versión con noticias no es independiente: lo que vale es la prueba en avance, y
  las tres lo dicen en `logica.revision`.

### 21.10 Criba de basura y medios por su acierto (30-10-2026, 02:00)

Lo pidió el usuario a la 01:30 («si hay medios que aciertan menos de un 50 % de las veces, FUERA»), con la prioridad de
acortar el triaje. Todo fijado por el contenido o por lo ya medido antes de cada fecha, sin mirar ningún precio con ello.

**Criba antes del triaje** (`noticias_historia.basura`, `Mesa` y `seleccion`):
- **Copias:** una pieza con el mismo titular (sin mayúsculas ni signos) que otra de su historia no suma medio. Son las
  notas de prensa sindicadas (Travala, Bybit, MEXC…) y la prensa local o las cadenas que repiten el texto (Newsquest,
  Reach, NBC locales, Big News Network). De agosto a octubre de 2026, **7.916 piezas** dejan de contar como medio.
- **Notas de prensa** por la URL (PR Newswire, GlobeNewswire, Chainwire, Business Wire, Accesswire, secciones
  `/press-release/`, `/sponsored`…) o por frases que solo usan ellas («prize pool», «giveaway», «press release»…).
- **Partes de guerra, terrorismo o sucesos sin lectura de mercado:** macro cuyos temas son solo guerra, geopolítica,
  política o catástrofe (o solo «geopolítica fuerte») y sin ninguna palabra de mercado en el titular (petróleo, bolsas,
  tipos, sanciones, aranceles, alto el fuego…). Un dato de empleo o de PIB no entra aquí.
- **Cuánto acorta:** con las copias fuera, los racimos de 3 o más medios casi no existen (de agosto a octubre de 2026: de
  cripto, 2,62 al día con ≥ 2 medios y 0,07 con ≥ 3; macro, 5,98 con ≥ 2, 0,58 con ≥ 3 y 0,10 con ≥ 4). La selección
  pasa a **cripto con ≥ 2 medios y macro con ≥ 3** (`RACIMO_N`, solo por volumen): ≈ 3,2 historias al día, **unas 3.900
  en 3 años en lugar de ≈ 8.400**. Con la cuota de 0,2, el triaje baja de ≈ 4 días a ≈ 1,5-2. En esos tres meses,
  además, la criba quita 7 partes de guerra y 3 notas de prensa que seguían siendo racimo.
- H34 pasa de `n` 3 a 2 (y su V2 de 5 a 3) antes de medir nada, para que sus variantes tengan noticias con esta escala.

**Medios por su acierto** (`familias/base.mascara_medios`; parámetro `medios` de `noticia_racimo` y `noticia_apertura`, y
`noticias_medios` del filtro común): el acierto del medio de la primera pieza de cada historia, ajustado al mercado a 4 h
como en `bot/fiables.py`, con sus historias de los días UTC completos anteriores (una historia, un caso).
- «sin_malos»: fuera si tiene muestra (≥ 20 historias en ≥ 10 días, como en vivo) y la cota **alta** de Wilson al 90 %
  queda por debajo del 50 %. Sin muestra, cuenta.
- «solo_buenos»: solo si tiene muestra, acierto ≥ 60 % y cota baja ≥ 50 %. Sin muestra, no cuenta.
- Seis hipótesis más (`h-20261030-0200-…-medios`): la V1 de cada una de las seis anteriores con «sin_malos» y con
  «solo_buenos»; las 12 pasan la admisión sin datos (44 variantes).
- En los 91 días medidos (`controles/fuentes/`) ningún medio de cripto llegó al 60 % ajustado: el informe dirá cuántos
  medios pasan cada corte en los 3 años y en esos 91 días (`medios_por_acierto`). Si no pasa ninguno, «solo_buenos» no
  dará señales y la admisión lo rechazará (A9).
- **Impulso fuerte con mayoría de noticias fiables (30-10-2026, 02:40):** modo «mayoria» de `noticias_previas` (solo si más de la mitad de todas las noticias de la ventana, también las neutrales, van a favor) y la hipótesis `h-20261030-0240-impulso_noticias-seis4r3-mayoria`: «6 diarias» con impulso ≥ 0,5 %, mayoría a favor de medios «solo_buenos», stop 3 %, 4R y 7 días; V2 «no en contra»; V3 impulso ≥ 1 %; V4 medios «sin_malos»; V5 6R; V6 12 h. En espera con las demás: 13 hipótesis y 50 variantes.
