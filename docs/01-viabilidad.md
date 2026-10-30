# Bot de trading fundamental: viabilidad, skills y plataforma

Fecha del análisis: 22-10-2026. Lo que no está verificado con una fuente oficial aparece marcado como **[verificar]**.

> No soy asesor financiero autorizado. Este documento es un análisis técnico y regulatorio de la viabilidad del sistema. Las criptos se eligen con los criterios que tú pusiste (volumen y volatilidad), medidos con datos reales; no es una recomendación de inversión.

---

## 1. Veredicto

**Sí es viable sin gastar tu límite semanal de Claude**, con dos condiciones:

1. **El bot no debe funcionar con tu suscripción.** Tiene que usar la **API de Claude con su propia clave**. Tu plan Max incluye **créditos de API mensuales**: 100 $ en Max 5x y 200 $ en Max 20x. Esos créditos no tocan tu cuota semanal de chat ni de Claude Code. Para activarlos, entra en claude.ai > Ajustes > Facturación > Créditos de API y vincula una organización de la Consola.
2. **El 90 % del trabajo lo hace código normal, sin IA.** Claude solo interviene en tres agentes: verificación, impacto y estrategia. Así el coste estimado es de **60–100 $ al mes**, que cubren los créditos incluidos.

Tu suscripción sigue siendo útil, pero solo para **construir** el sistema conmigo en Claude Code.

### Por qué no con la suscripción

| Motivo | Detalle |
|---|---|
| Condiciones de uso (Consumer Terms, 8-oct-2025) | §3.7 prohíbe el acceso automatizado "through a bot, script, or otherwise" salvo con clave de API. §3.9 prohíbe usar el servicio "to buy or sell securities" o para recibir asesoramiento sobre productos financieros. Las condiciones de la API (Commercial Terms) no tienen esa cláusula. |
| Límites | Los límites semanales no se publican en números y existen precisamente para frenar el uso continuo 24/7. Un bot de 8 agentes los agotaría y te dejaría sin Claude para trabajar. |
| Tareas programadas | Las rutinas en la nube tienen un intervalo mínimo de 1 hora y las de escritorio necesitan el PC encendido. Todas consumen tu cuota. No sirven para reaccionar en segundos. |

### Política de uso (en vigor desde el 12-nov-2026)

La nueva política de uso de Anthropic clasifica como **área de alto riesgo** cualquier "personalized recommendation to buy, sell, hold… digital assets". Exige que una persona cualificada revise de verdad la recomendación con autoridad para cambiarla. Lo prudente es que **tú apruebes las órdenes**. La web ya incluye ese paso, con tres modos:

- Apruebo cada orden.
- Ventana de veto de 30 s.
- Automático, solo para paper.

En real, recomiendo los dos primeros.

---

## 2. Coste estimado de IA (precios de API a 22-10-2026)

| Agente | Motor | Llamadas/día (día tipo) | Coste/llamada | Coste/día |
|---|---|---|---|---|
| Cazadores (captura y filtro) | Código | miles | 0 | 0 $ |
| Verificador | Haiku 5.5 (0,10 / 0,50 $ por M de tokens) | ~400 | ~0,0002 $ | ~0,08 $ |
| Analista de impacto | Sonnet 5.5 (2 / 10 $) | ~60 | ~0,012 $ | ~0,70 $ |
| Flujo minorista | Código | continuo | 0 | 0 $ |
| Estratega + plan de cada apertura | Opus 5.5 (4 / 20 $) | ~30 | ~0,046 $ | ~1,40 $ |
| Riesgo y Ejecutor | Código | — | 0 | 0 $ |
| Revisión diaria (post-mortem) | Opus 5.5 por lotes (−50 %) | 1 | ~0,30 $ | ~0,30 $ |
| **Total** | | | | **≈ 2,5 $/día ≈ 75 $/mes** |

- Si un día hay tormenta de noticias, el gasto puede multiplicarse por 2 o por 3.
- Se pone un **límite de gasto** en la Consola de Claude para que nunca pase de los créditos.
- La caché de prompts abarata la parte repetida de cada llamada (instrucciones, reglas, contexto) a una fracción del precio.

---

## 3. La realidad de la velocidad (importante)

Los bots de alta frecuencia reaccionan a un titular en milisegundos. Una llamada a un modelo tarda entre 0,5 y varios segundos. **No vamos a ganar el primer tic de precio.** La ventaja de este sistema está en otros cuatro sitios:

1. **El plan previo a cada apertura.** El estratega llega a la apertura con la noticia ya verificada y analizada, y con la entrada preparada. A la hora en punto, el código ejecuta en milisegundos.
2. **El segundo movimiento.** Decide si el movimiento continúa o se agota entre segundos y minutos después del titular, que es donde el humano y el bot rápido se equivocan.
3. **No caer en noticias falsas.** Algunos casos reales que el verificador debe cazar:
   - Enero de 2024: el tuit falso de la SEC sobre la aprobación del ETF.
   - Octubre de 2023: el falso ETF de BlackRock publicado por Cointelegraph.
   - 7 de abril de 2025: la falsa "pausa de aranceles de 90 días".
4. **Reglas deterministas para lo obvio.** Una fuente T0 (oficial o del exchange) con un patrón conocido puede disparar una orden por regla, sin esperar a la IA.

Sobre "replicar al minorista": no hay estudios revisados por pares que demuestren que seguir o ir contra el posicionamiento minorista funcione. Es una **hipótesis que hay que probar**. El diseño ya incluye la excepción clave: cuando el minorista está en un extremo (ratio L/S muy alto o funding alto), el estratega no le sigue, porque es cuando suelen barrerse los stops.

**Datos reales que apoyan la idea de las aperturas** (Binance, 62 días):

| Momento | Hora UTC | Rango medio de la vela de 1 h de BTC |
|---|---|---|
| Apertura de Nueva York | 14:00 | **0,93 %, 1,95 veces la hora mediana** |
| Datos macro de EE.UU. y preapertura | 12:00–13:00 | 1,4–1,7 veces la hora mediana |
| Londres | 08:00 | 1,3 veces |
| Asia | 00:00–02:00 | 1,1 veces |

---

## 4. Plataforma para operar desde España

Contexto regulatorio [verificado con prensa jurídica y avisos de ESMA y la CNMV; los detalles de cada exchange, **verificar**]:

- **Transición MiCA en España terminada (1-jul-2026).** Solo puedes operar en exchanges con licencia MiCA (CASP).
- **USDT no se negocia en ningún exchange con licencia MiCA del EEE**, porque Tether no pidió autorización. **Cambiamos USDT por USDC** (o por EUR). Por eso la web ya usa USDC.
- **Perpetuos = CFD para el minorista español** (CNMV, junio de 2026). El apalancamiento queda en 2:1 como máximo.
- **Binance y Bitget no sirven**: no tienen licencia MiCA, y Binance está cerrando su operativa en el EEE. Sus **datos públicos** sí se pueden leer para medir el flujo minorista global.

| Opción | Por qué | Pegas |
|---|---|---|
| **1. OKX EU (recomendada)** | Licencia MiCA (Malta) y MiFID. Comisiones de contado de 0,08/0,10 % solo con la cuenta X-Perps desbloqueada; sin ella, 0,20/0,35 % (docs/05, sección 21). Órdenes por WebSocket y modo demo para paper. X-Perps reguladas que permiten **ponerse corto** cuando la noticia es bajista. Soportada por CCXT (`myokx`). | El libro de órdenes es solo del EEE, con menos profundidad. Comisiones de la UE y acceso al demo desde el EEE **[verificar]**. Sanción del Departamento de Justicia de EE.UU. en 2025. |
| 2. Bitvavo | Licencia MiCA (Países Bajos). La comisión más baja: 0,05/0,05 % en pares USDC. El libro en EUR más profundo de Europa. API rápida. | Solo contado, así que no se puede ir corto. Sin entorno de pruebas público. |
| 3. Kraken | Licencia MiCA (Irlanda). Libro de órdenes global. Órdenes por WebSocket y FIX. | Comisiones de 0,40/0,80 % desde julio de 2026 con volumen bajo, que se comen una estrategia de noticias. |
| Hyperliquid | Técnicamente la mejor para perpetuos. | Sin licencia: zona gris legal. No usar sin asesoría jurídica. |

**Plan propuesto:**

1. Paper trading en el modo demo de OKX EU.
2. Real en OKX EU, con contado más X-Perps a 1× o 2× para poder ir corto.
3. Bitvavo como plan B solo de contado.

**Impuestos:**

- Cada venta o permuta, incluido pasar de cripto a USDC, tributa en IRPF (base del ahorro, del 19 % al 30 %).
- Un bot genera miles de operaciones, así que exportaremos un registro fiscal desde el primer día.
- Modelo 721 si superas 50.000 € en exchanges extranjeros a 31 de diciembre.
- Con DAC8, Hacienda recibirá los datos de cada operación.

---

## 5. Criptos: medición real (Binance, 22-10-2026)

| Cripto | Volumen/día (30 d) | Volatilidad anual | Rango diario | Beta frente a BTC | Papel |
|---|---|---|---|---|---|
| BTC | 1.260 M$ | 37,4 % | 2,74 % | 1,00 | Fija |
| ETH | 656 M$ | 51,9 % | 3,77 % | 1,23 | Fija |
| **SOL** | 249 M$ | 55,7 % | 4,48 % | 1,24 | **Elegida**: 3.ª en volumen, mucha noticia propia (ETF, caídas de red) |
| **XRP** | 194 M$ | 67,7 % | 5,11 % | 1,60 | **Elegida**: 4.ª en volumen y la de mayor beta, la que más amplifica |
| SUI | 84 M$ | 86,7 % | 6,69 % | 1,58 | Reserva de rotación |
| DOGE | 75 M$ | 65,5 % | 5,16 % | 1,45 | Reserva de rotación |
| USDC | — | — | — | — | Moneda de reserva, en lugar de USDT |

**Regla de rotación, revisada cada semana por código:**

- Puntuación = volumen × rango diario, sobre las criptos disponibles en el exchange elegido.
- Una cripto entra si supera durante 2 semanas seguidas a la más débil de las elegidas.
- Tiene que tener un volumen diario mínimo de 50 M$.
- Su diferencial medio en el exchange tiene que ser menor de 5 pb.

---

## 6. Skills necesarias

### A. Skills de Claude que crearemos (instrucciones y reglas que cargan los agentes con IA)

| Skill | Agente | Contenido |
|---|---|---|
| `verificacion-noticias` | Verificador | Escala de fuentes T0–T3, reglas de corroboración (T0, o 2 fuentes T1 independientes en N segundos), señales de alarma (lenguaje especulativo, cuentas nuevas, falta de confirmación en el dominio oficial), banco de casos falsos y verdaderos para probarlo. |
| `impacto-fundamental` | Analista | Plantillas por tipo de evento: IPC, empleo, FOMC, ETF, listados, hackeos, caídas de red, on-chain. Betas por cripto, umbrales (≥0,8 % y confianza ≥55 %), horizonte, salida en JSON estructurado. |
| `flujo-minorista` | Estratega (contexto) | Cómo leer funding, ratio L/S, OI, CVD y las primas de Coinbase y Kimchi. Cuándo seguir y cuándo no (extremos). |
| `estrategia-aperturas` | Estratega | Manual de cada ventana: Tokio, Londres, macro de EE.UU., Nueva York, CME. Ruptura del rango de los 5 primeros minutos, VWAP, stops por volatilidad horaria, objetivos de 1,5 R y 3 R, tamaño por riesgo, invalidación. Ticket en JSON. |
| `revision-diaria` | Auditor (nuevo) | Post-mortem de cada día: aciertos y errores por tipo de noticia y por ventana, y ajustes propuestos que tú apruebas. |

### B. Capacidades técnicas (código Python, sin IA)

| Módulo | Herramientas |
|---|---|
| Captura de noticias | Fuentes oficiales: RSS e ICS de BLS, Fed y BEA, EDGAR (gratis). Anuncios de exchanges por sondeo. Telegram con Telethon (gratis). Tree News WebSocket (gratis con retraso; 500 $/mes sin retraso). X de pago por uso (0,005 $ por post leído, solo unas pocas cuentas). Mercados de predicción por WebSocket. |
| On-chain | Whale Alert (≈30 $/mes) y Etherscan gratis. |
| Flujo minorista | Endpoints públicos gratuitos de Binance, Bybit y OKX (funding, OI, L/S, liquidaciones), CVD calculado a partir de las operaciones, primas calculadas. |
| Ejecución | CCXT (WebSocket incluido) o `python-okx`. NautilusTrader si pasamos a algo más serio. |
| Riesgo | Motor de reglas propio: límites, interruptor de parada, pausa macro. |
| Bus de eventos y web | FastAPI con WebSocket hacia la web (el formato de mensajes está en el apartado 9) y almacenamiento en SQLite o DuckDB. |
| IA | SDK oficial `anthropic` para Python, con caché de prompts, salidas estructuradas y límite de gasto. |
| Pruebas | Reproducción de días pasados (noticias más velas) para probar antes de arriesgar dinero. |
| Aprobación desde el móvil | Bot de Telegram con botones de aprobar y rechazar, además de la web. |

### C. Herramientas ya disponibles que aprovecharemos

- Skill `claude-api` (instalada): para escribir bien las llamadas a Claude.
- CoinGecko MCP (oficial y gratis): contexto de mercado para el analista. Fuera del camino rápido.
- OKX Agent Trade Kit, con modo de solo lectura **[verificar]**.
- Frameworks abiertos de referencia: CCXT, NautilusTrader. Freqtrade y Jesse no encajan, porque trabajan por cierre de vela.

---

## 7. Costes totales previstos

| Concepto | Mínimo viable | Profesional |
|---|---|---|
| IA (API de Claude) | 60–100 $/mes, cubiertos por los créditos de Max | igual |
| Noticias y datos | 0–80 $/mes | 1.000–3.500 $/mes (Tree Sapling, Coinglass, LunarCrush…) |
| Servidor 24/7 | VPS en Fráncfort o Ámsterdam, 6–10 €/mes | 2 servidores (UE + Tokio) |

Se empieza por el mínimo viable. Solo se paga más si los datos de paper demuestran que la velocidad extra da dinero.

---

## 8. Hoja de ruta propuesta

1. **Base** (sin dinero real):
   - Captura de fuentes gratuitas, bus de eventos, web conectada en vivo.
   - Datos en `M:\OneDrive\Bot de trading fundamental\data`, nunca en `C:`.
2. **Cerebro:**
   - Verificador, analista y estratega con sus skills.
   - Banco de pruebas con noticias falsas y verdaderas históricas.
3. **Paper trading 4 semanas** en el demo de OKX EU, con aprobación humana.
   - Criterio para pasar a real: más de 40 operaciones, R medio positivo después de comisiones, caída máxima menor del 6 % y ningún fallo de riesgo.
4. **Real pequeño:**
   - 0,25 % de riesgo por operación durante el primer mes.
   - Servidor 24/7.
   - Registro fiscal.

---

## 9. Formato de mensajes entre el bot y la web

La web (`dashboard/radar.html`) ya acepta mensajes por WebSocket con el mismo formato que usa su simulación:

```json
{"type":"event","ts":1792700000000,"story":"s123","agent":"verifier","kind":"verified",
 "title":"Titular","text":"Veracidad 92/100 · bls.gov lo publica","coins":["BTC"],
 "patch":{"verify":{"score":0.92,"checks":[{"ok":true,"t":"bls.gov publica la cifra"}]}},
 "llm":{"model":"Haiku 5.5","tin":2300,"cached":1700,"tout":230,"cost":0.0002}}
```

- `agent`: hunter · verifier · impact · flow · strat · risk · human · exec.
- `kind`: captured · filtered · verified · unverified · impact · noimpact · flow · brief · strategy · nostrategy · riskok · riskko · humanwait · humanok · humanko · scheduled · order · filled · blocked · manage · closed.
- Otros mensajes: `{"type":"market","prices":{...},"flow":{...}}` y `{"type":"agent","agent":"exec","patch":{...}}`.
