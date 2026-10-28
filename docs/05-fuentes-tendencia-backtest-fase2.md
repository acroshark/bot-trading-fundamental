# Fase 1b y fase 2: fuentes masivas, tendencia en vivo, backtest, motor de aperturas, paper trading y agentes con Claude

Estado a 26-10-2026, ampliado el 27-10-2026 con el laboratorio de 2.000 estrategias, el motor v3 y el sistema que aprende (secciones 13 a 15). Todas las cifras salen de datos reales y, salvo que se diga lo contrario, de la parte **fuera de muestra**: el último 30 % de los datos, que no se usó para elegir ningún parámetro. Todo lo que opera es **paper** (dinero ficticio con precios reales de OKX EU).

## 1. Por qué salía «0 noticias de importancia»

- **Pocas fuentes y casi todas cripto o de EE.UU.:** había 16.
- **Un filtro estrecho:** descartaba la geopolítica (guerras, elecciones, la guerra comercial entre China y EE.UU.) si no nombraba una cripto o un término macro en inglés.
- **Solo contaba lo ya puntuado:** «importancia 6+» se limitaba a lo que la IA local ya había puntuado, y la IA local solo veía lo que dejaba pasar ese filtro.

**Arreglo:**
- 5.714 fuentes verificadas en 10 regiones.
- Temas de mercado en 14 idiomas: elecciones, guerra, sanciones, aranceles, bancos centrales, energía, catástrofes… Los titulares en chino y japonés se comparan por pares de caracteres.
- Las noticias de hasta 6 horas entran como **contexto** para preparar cada apertura, marcadas como tales.
- La IA local tría primero lo fresco e importante y, a igual prioridad, lo más reciente.

## 2. Fuentes por cercanía de bolsa

| Región | Bolsa de referencia | Fuentes verificadas |
|---|---|---|
| Japón | Tokio | 382 |
| China continental | Shanghái | 433 |
| Hong Kong y Taiwán | Hong Kong | 414 |
| Corea del Sur | Seúl | 523 |
| Alemania y zona euro | Fráncfort | 855 |
| Reino Unido e Irlanda | Londres | 650 |
| España y Portugal | Madrid | 431 |
| EE.UU. y Canadá | Nueva York | 849 |
| Latinoamérica | São Paulo | 610 |
| Oriente Medio, Ucrania y Rusia | Dubái (geopolítica) | 567 |
| **Total** | | **5.714** |

**Cómo se verificaron:** cada fuente se descargó de verdad. Solo entran las que responden, se leen como RSS o Atom y tienen noticias de los últimos 30 días. Se usan los certificados de Windows, que recuperaron 168 webs oficiales asiáticas que fallaban por SSL.

**El lector se identifica como bot («BotFundamental/0.1»):**
- Unas 35 fuentes (CBC, Nasdaq, Tesoro de EE.UU.) bloquean a los lectores automáticos, y se respeta su decisión: su contenido llega por Google News.
- Reuters, AP, CNN, Washington Post, Banxico y otros grandes no tienen RSS público que funcione.

**Consulta:**
- Hay un único planificador con peticiones condicionales e intervalos que se adaptan.
- Hay un límite por servidor.
- La lectura de los feeds va en procesos aparte para no frenar el bot.

**Dos feeds del mismo medio no cuentan como confirmación.** Una noticia confirmada por otros medios gana credibilidad, y si la publican varias regiones gana alcance.

## 3. Notas de cada noticia

| Nota | Escala | Significado |
|---|---|---|
| Credibilidad | 0–100 % | Probabilidad de que sea cierta. La API da además una credibilidad ajustada por el nivel de la fuente y las confirmaciones. |
| Alcance | local, nacional, regional o global; 0–100 | A cuántos mercados llega |
| Temporalidad | minutos a estructural; urgencia 0–100 | Cuánto dura el efecto |
| Repercusión | dirección de −100 a +100 y magnitud de 0 a 100, con activos y bolsas | Cómo podría afectar |
| Novedad | 0–100 | 0 si ya se sabía o estaba descontado |
| Importancia | 0–10 | Nota global; por debajo de 4 se archiva |

**Ejemplo real:** «Irán amenaza con una guerra mundial…» salió con importancia 8/10 pero credibilidad 45 %. Venía de un informe de prensa sin confirmar, así que el verificador la marcó como no verificada y no entra en la balanza.

## 4. Tendencia en directo

- **Bolsas:** índices de 10 bolsas cada 60 s. En el globo, cada ciudad lleva su barra verde o roja: en directo si está abierta, la última sesión si está cerrada y la previsión si abre pronto.
- **Cripto:** libro de órdenes de OKX EU (desequilibrio en 5 niveles) y operaciones agresivas de Binance (CVD a 1, 5 y 15 min, proporción de compras, volumen frente a lo normal y tendencia).
- **Vista Mundo:**
  - **A la izquierda:** cada instrumento con su volumen en directo, compras frente a ventas, libro, minorista y la **señal del sistema**: COMPRA, VENTA o NO OPERAR, y si es buen momento, con los motivos.
  - **A la derecha:** la **balanza de noticias**, con las que más empujan a compra o venta en las últimas 6 h.

## 5. Backtest de estrategias generales

**Muestra:** 690 estrategias con 10 años de índices y toda la historia de las 4 monedas, con comisiones.

**Criterios:** corrección por comparaciones múltiples (Benjamini-Hochberg) y un margen de 15 min tras cada cierre.

**Resultado:**

| Veredicto | Estrategias |
|---|---|
| Funciona | **0** |
| Dudosa | 8 (las 7 operables pierden entre −0,04 % y −0,15 % por operación tras comisiones) |
| Solo correlación | 143 (bolsas que se mueven juntas, pero sin tiempo para operar con ello) |
| No funciona | 465 |
| Pocos datos | 74 |

## 6. Motor de aperturas (tu operativa)

La regla es la señal → confirmación en los primeros minutos de la apertura → entrada → stop dinámico → reentradas mientras haya volumen.

**Datos:**
- Velas de 1 minuto de Binance de 24 meses (del 01-10-2024 al 23-10-2026): 2.614 aperturas reales × 4 monedas.
- Ventanas agrupadas: Tokio y Seúl, China, Europa, São Paulo y Nueva York.
- Costes: 0,10 % de comisión + 2 pb de deslizamiento por lado.
- Fuera de muestra desde el 14-03-2026.

### 6.1 Qué hace la apertura con las cripto

- **Volumen sí:** en Nueva York, el volumen de los 30 min posteriores es 2,3 veces el de antes y el movimiento de 4 h casi se duplica. En Tokio ronda 1,4 veces y en Europa no hay efecto.
- **Dirección no:** el retorno medio de las 4 h siguientes va de −0,12 % a +0,22 %, y sube en el 45–52 % de los días. Coincide con un estudio de alta frecuencia de la universidad noruega NTNU.
- **Techo absoluto:** acertando siempre la dirección desde la apertura, sin stops y con costes, se gana de media entre +0,34 % y +1,50 % por apertura según ventana y moneda.
- **+3 % neto en 4 h:** solo pasa en el 0,4 % (Europa, BTC) al 15,1 % (Nueva York, SOL) de las aperturas.

### 6.2 Versión 1 (stop del 0,3 % y break-even a +1R)

Pierde siempre; incluso el «oráculo», que conoce la dirección real, pierde −0,06 % por apertura. **Causa:** el break-even y el stop dinámico cerraban casi todo en −0,24 %, que son justo las comisiones.

### 6.3 Versión 2 (disciplina de salida de day-trader-skill)

Se probaron 144 salidas:
- **Stop:** en el medio del rango, en el otro lado o con ATR de 5 min, siempre con un suelo de costes.
- **Objetivos:** 2R, 3R, +3 % neto o sin tope.
- **Break-even:** nunca, a +1,5R o a +2R, cubriendo costes.
- **Parciales y confirmación:** con y sin salida parcial, y confirmación al cierre de 1 o de 5 min.

Además, una puerta de probabilidades con rupturas pasadas, la bandera de «movimiento ya hecho» y límites diarios.

**Elegida solo con el 70 % inicial:** stop en el otro lado del rango (0,96 % mínimo), objetivo +3 % neto, break-even cubriendo costes a +1,5R, confirmación al cierre de 5 min, rango de 30 min y volumen relativo ≥ 2.

| Variante (fuera de muestra) | Operaciones | Ganadoras | R medio | Por operación |
|---|---|---|---|---|
| Encadenamiento (señal de la bolsa anterior) | 303 | 36 % | −0,20 | −0,22 % |
| Ruptura libre (sin señal) | 457 | 35 % | −0,22 | −0,24 % |
| Oráculo (**techo inalcanzable**) | 478 | — | +0,13 | +0,13 % |

- **Cómo terminan:** el 71 % por tiempo, el 24 % en el stop inicial y el 4 % en el objetivo. Solo el 4 % llega a +3 % neto antes del stop.
- **Solo Nueva York con SOL, ETH y XRP:** −0,06 R en 36 operaciones, que no se distingue del azar.
- **Puerta de probabilidades:** con 60 sesiones nunca hay casos suficientes. Con 250, deja pasar 5 operaciones, también indistinguibles del azar.

### 6.4 Cuenta paper día a día (fuera de muestra, 160 días)

Supuestos: 10.000 USDC, 0,5 % de riesgo en el stop por operación, tope de nominal del 40 %, una posición por moneda y 3 entradas al día como máximo.

| Variante | Media/día | Mediana/día | Peor día | Días ≥ +1 % | Días ≥ +3 % | Total |
|---|---|---|---|---|---|---|
| Encadenamiento 1:1 | −0,16 % | −0,11 % | −1,61 % | 5 % | 0 % | −22,6 % |
| Encadenamiento 2:1 | −0,32 % | — | −3,21 % | 11 % | 3 % | −40,6 % |
| Oráculo 1:1 (**techo inalcanzable**) | +0,16 % | — | — | 11 % | 1 % | +29,1 % |

El 2:1 es el máximo para particulares en derivados en España. Son las mismas operaciones con el doble de tamaño: amplifica las pérdidas igual que las ganancias.

### 6.5 Qué significa

- **Ninguna configuración operable gana con costes.** Las reglas de precio y volumen en la apertura no dan ventaja por sí solas.
- **La pieza que falta medir es la señal en vivo:** noticias verificadas, plan del estratega y predicciones. No tiene historia para un backtest. Por eso el bot opera en paper con esa señal y guarda cada resultado: con varias semanas sabremos si aporta.
- **El objetivo de +3 % por apertura** no tiene base en estos datos: ni con la dirección perfecta se alcanza de forma habitual. Esto no es una recomendación de inversión; es lo que miden los datos.

## 7. Predicciones

Cada 5 minutos, la probabilidad de subida de cada bolsa que va a abrir y de cada cripto en su próxima ventana.

- **Cómo se calcula:** parte de la tasa base y se ajusta con factores visibles (bolsas anteriores según el backtest, futuros, noticias, flujo de órdenes y minorista extremo).
- **Cómo se mide:** todas se guardan y se comprueban, así que el acierto real queda a la vista.
- **No son órdenes.**

## 8. Paper trading

Precios reales de OKX EU, comisiones y deslizamiento.

**Salidas configurables por operación:**
- stop inicial;
- break-even que cubre costes, cuando se pide;
- stop dinámico;
- objetivo, con salida parcial opcional;
- cierre por tiempo.

**Controles de riesgo:**
- 0,5 % por operación, contando las comisiones;
- pérdida diaria máxima del 3 %;
- 4 posiciones como máximo y **una sola por moneda**;
- diferencial máximo;
- pausa ante datos macro.

**Desde la web en directo** puedes:
- cambiar el modo de aprobación (automático, veto de 30 s o manual);
- aprobar o rechazar cada orden;
- cancelar programadas;
- cerrar posiciones;
- detener o reanudar el paper.

## 9. Fase 2: agentes con Claude

| Agente | Modelo | Cuándo |
|---|---|---|
| Verificador | Haiku 5.5 | Noticias con importancia local de 5 o más |
| Analista de impacto | Sonnet 5.5 | Verificadas con importancia de 6 o más |
| Estratega | Opus 5.5 | 15 min antes de cada ventana de apertura, y tras un impacto grande |

- **Sin clave de API** (como ahora): el verificador y el analista funcionan con Qwen en local, gratis, y el estratega usa reglas basadas en las predicciones.
- **Presupuesto diario:** 3 $ por defecto, contando el peor caso de cada llamada.
- **Para activarlo:** créditos de API de tu plan Max y la variable `ANTHROPIC_API_KEY`. Las dos cosas solo puedes hacerlas tú.

## 10. IA local y la fábrica

- **Sin memoria de vídeo para el servidor propio:** el bot usa el Qwen 3.5 9B de tu fábrica (puerto 8082), con estas reglas:
  - solo cuando su hueco está libre;
  - nunca con `gpu.lock`;
  - nunca lo apaga.
  - `BOT_LOCAL_AI_SHARE=0` lo desactiva.
- **Pendiente de tu decisión:** para que la fábrica recupere la GPU cuando la necesite, su `fabrica\lib\gpu.py` debería reconocer también el alias `qwen-trading` del servidor del bot.

## 11. Skills de day trading (day-trader-skill, MIT)

**Instaladas:** 10 de las 11 skills de [Jakeschincariol/day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill) en `.claude/skills` del proyecto, tras auditar entero su código y sus instrucciones.
- No hay código malicioso.
- Solo consultan Yahoo, la SEC, Nasdaq y Stooq.

**No instalada:** `trader-broker`, que puede mandar órdenes reales a Alpaca o IBKR.

**Uso:** están pensadas para acciones de EE.UU. y bloquean las cripto. Su valor para el bot fue el método de salida, que se probó en la versión 2 del motor (sección 6.3).

## 12. Revisión adversarial

**Cómo se hizo:** cinco revisores revisaron la integración y otro verificó cada hallazgo intentando refutarlo. Se confirmaron 51 de 54, todos corregidos.

**Los más importantes:**
- **Caída del bot:** se caía al cerrar un servidor de IA heredado (`netstat` en español).
- **Seguridad:** el WebSocket aceptaba órdenes de cualquier web y `"ok":"false"` contaba como aprobación.
- **Web en directo:** no permitía controlar el paper.
- **Rendimiento:** bloqueos con 5.700 feeds.

## 13. Laboratorio de 2.000 estrategias (26-10-2026)

Fuente: `BACKTEST_DIR/laboratorio.json` (`bot/laboratorio.py`). Todas cumplen tus reglas: stop ≥ 1,5 %, objetivo ≥ 2R, sin break-even ni stop dinámico.

**Datos y costes:**
- 3.771 aperturas reales × 4 monedas, del 01-11-2023 al 23-10-2026, con velas de 1 min de Binance al contado.
- Muestra hasta el 16-08-2025 (60 %) y fuera de muestra desde ahí (40 %).
- Costes: 0,10 % por lado, 2 pb de deslizamiento, 5 pb más en los stops de BTC y ETH y 20 pb en los de SOL y XRP. El objetivo solo cuenta si el precio lo supera en 2 pb.

**Resultado:**

| | Estrategias |
|---|---|
| Probadas | 2.000 |
| Con ≥ 30 operaciones fuera de muestra | 1.358 |
| Positivas dentro de muestra / fuera / en las dos | 285 / 452 / 63 |
| Baten a su base emparejada (dirección al azar con su cuota de largos) | 68 de 1.358 (por azar se esperan unas 68) |
| De las positivas en muestra, baten a su base | 5 de 172 (por azar, unas 9) |
| Prometedoras | **2** |
| Robustas (pasan todas las pruebas contra la suerte) | **0** |

- **Las pruebas contra la suerte:** base emparejada, permutación por bloques, Benjamini-Hochberg al 10 %, Sharpe deflactado con 381 pruebas efectivas de 2.000, vecinos de la rejilla y positiva en cada año fuera de muestra. Ninguna estrategia las pasa todas: con estos datos ninguna se puede dar por buena.
- **Cuenta honesta en avance** (cada trimestre se eligen las 10 mejores con lo anterior; 10.000 USDC, 0,5 % de riesgo, desde el 01-04-2024, 668 días): **−61,17 %**, con una caída máxima del 65,28 %. Eligiéndolas cada año: −68,74 %. Las elegidas dieron −0,118 R en el trimestre siguiente (3.113 operaciones; 3 de 11 trimestres positivos).
- **Régimen:** en la muestra todo subió (BTC +231 %, XRP +406 %); fuera de muestra todo cayó (BTC −31 %, ETH −46 %, SOL −45 %, XRP −58 %). El 56 % de las operaciones fuera de muestra de las prometedoras son cortos.
- **Tu idea de los +3 %:** en el 89 % de las aperturas alguna moneda se mueve ≥ 3 % en las 24 h siguientes, y con la dirección perfecta una horquilla de 1,5 % / 3 % cobraría el objetivo el 50 % de las veces (25 % con dirección al azar). El movimiento existe; lo difícil es saber de antemano hacia dónde.

**Las 2 prometedoras** (no son una ventaja demostrada: el criterio no se fijó antes de ver los datos):

| Estrategia | Fuera de muestra | Dentro | Falla |
|---|---|---|---|
| **E1** · Tendencia diaria de BTC (media de 20 días) · reanudación tras retroceder la mitad del impulso de 15 min · volumen relativo ≥ 1,5 · solo Asia y SOL · SL 3 % · TP 2R | 34 operaciones, **+0,205 R**, 56 % ganadoras | 57 op., +0,094 R | Benjamini-Hochberg, Sharpe deflactado y vecinos |
| **E2** · Movimiento de los primeros 15 min (±0,2 %) · entrada a +30 min · volumen relativo ≥ 1,5 · solo a favor de la tendencia de BTC · todas las aperturas y monedas · SL 3 % · TP 3R | 278 operaciones, **+0,140 R**, 52 % ganadoras, 44 % largos | 557 op., +0,044 R | Benjamini-Hochberg (q 1,0) y Sharpe deflactado; la permutación sí la pasa (p 0,002) |

- La cuenta en muestra con las dos (+17,59 % en 309 días, caída máxima del 10,89 %) está inflada por la selección: la estimación honesta es la cuenta en avance de arriba.
- En una pasada anterior desde 2021, las dos prometedoras de hoy dieron una mediana de −0,069 R en aquella muestra (ninguna positiva). Prometedoras en las dos pasadas: 0.
- **Lista congelada el 26-10-2026** (`laboratorio-congelado-2026-10-26.json`): el único fuera de muestra virgen es lo que pase desde entonces. Lo mide la vigilancia (sección 15).

## 14. Motor de aperturas v3: modo impulso (27-10-2026)

El motor en vivo (`bot/apertura.py`, modo «impulso», por defecto) opera la regla de **E2** con tus reglas: mide el impulso de los primeros 15 min de cada apertura y, si pasa de ±0,2 %, lo sigue a +30 min con volumen relativo ≥ 1,5 y la tendencia diaria de BTC a favor. Horquilla pura de 3 % y 3R (+9 %), sin break-even, stop dinámico ni parciales, y solo si el objetivo deja ≥ 3 % neto de costes y funding. Largos al contado y cortos como perpetuos (el contado USDC no admite cortos). La diferencia con el laboratorio: sin tope de tiempo, con 7 días de seguridad.

**Réplica con datos reales** (`BACKTEST_DIR/controles/compara_impulso-2026-10-27.txt`):
- **Con la salida del laboratorio** (cierre en la misma apertura del día siguiente): las 278 operaciones fuera de muestra coinciden en moneda, hora y lado, y las 278 salidas en hora y motivo (|ΔR| máximo 0,0005); R medio +0,140 en los dos. En la muestra, 561 frente a 557 (+0,045 frente a +0,044).
- **Con 7 días de seguridad (lo que corre en vivo):** 202 operaciones fuera de muestra, **R medio +0,346** (113 en el stop, 60 en el objetivo y 29 por tiempo; 65 h de media). Largos +0,098 R (85 op.); cortos +0,526 R (117 op.). Con la gestión del paper, +0,373 R, y +0,375 R con el funding real de los perpetuos. En la muestra, 469 operaciones y +0,130 R.
- **Ojo:** la variante de 7 días se eligió después de ver estos resultados y el laboratorio nunca la validó: no demuestra nada. Los cortos ganan en un periodo en que todo cayó.
- Por qué no entra: impulso menor de ±0,2 % (7.446 veces), BTC no acompaña (4.581) y volumen relativo < 1,5 (1.925).

**Otros detalles:**
- **Límites diarios:** 4 entradas y 2 % de riesgo. Las 4 monedas se mueven juntas y suelen disparar en la misma apertura.
- **Comisión del perpetuo:** se supone la misma que la del contado (0,10 % por lado), la prudente. El funding sale de Binance escalado a 8 h con su periodo real (`funding_h`); no es el de OKX X-Perps EU, que está sin confirmar.
- **Noticias:** no dan la dirección. Filtran (`no_contra`: no se entra si van claramente en contra) y etiquetan cada operación (a favor, en contra, sin noticia) para medir si aportan.
- **Variables de entorno:** `BOT_APERTURAS_ESTRATEGIA=ruptura` vuelve a la v2, y `BOT_IMPULSO_PARAMS` (JSON) cambia parámetros sobre el preset. Si la evolución ha aplicado una estrategia (`estrategia_activa.json`), manda esa.

## 15. El sistema que aprende (27-10-2026)

Cuatro módulos que miden con los datos que van llegando y proponen cambios. Ninguno opera dinero real ni cambia tus reglas. Con poca muestra no concluyen nada, y lo dicen.

**Vigilancia** (`bot/vigilancia.py`):
- Prueba en avance, sin operar, de las estrategias congeladas: E1 y E2 con su salida del laboratorio, y E1-7d y E2-7d con los 7 días de seguridad (E2-7d es la que opera el paper).
- Cuenta desde el 27-10-2026 (la medianoche siguiente a la congelación) con las mismas funciones y costes del laboratorio, y compara lo vivo con el backtest fuera de muestra.

**Aprendizaje** (`bot/aprendizaje.py`):
- Mide cada noticia triada con lo que hizo el precio después (+15 min, +1 h, +4 h y +24 h, la sesión de la apertura y tu horquilla de 3 % y 3R).
- De ahí saca el acierto de cada medio, región, idioma, tipo de noticia y ventana, y un **peso por medio** (0 a 1,5; 1 = neutro) que multiplica la credibilidad de sus noticias en la señal.
- El acierto se ajusta al mercado: si casi todo es bajista y el mercado cae, acertar «bajista» no tiene mérito. Hacen falta 20 casos independientes (bloques de 4 h) en 10 días distintos, y el peso solo se aparta de 1 si el intervalo del 90 % excluye el 50 %.
- **Prueba en seco con la base real:** 470 noticias con dirección caían en solo 3 horas distintas (los reinicios captan oleadas de contexto en el mismo minuto). Acierto bruto del 75 %, pero la tendencia del mercado ya daba el 59 %. Ajustado, 52 %, con un intervalo del 90 % de 28 a 75 %. No se concluye nada y todos los pesos siguen en 1, que es lo correcto.
- Escribe un post-mortem de cada operación cerrada (paper y vigilancia) con la IA local, en baja prioridad.

**Fuentes vivas** (`bot/fuentes_vivas.py`):
- Ciclo de vida de cada feed y de cada medio: prueba → activa → silenciada → retirada, con reglas prudentes: ≥ 30 casos independientes, como mucho 3 silencios por revisión y 10 al día, y las oficiales solo por caída técnica.
- Las silenciadas se siguen leyendo y midiendo (pueden volver), pero no cuentan en la señal ni gastan IA de pago.
- Descubre medios que solo llegan por agregadores y dan las noticias antes que nadie o aciertan. Les busca el RSS respetando robots.txt (12 peticiones por hora como mucho) y los añade en prueba.

**Evolución** (`bot/evolucion.py`):
- Historial de todo lo que cambia (fuentes, pesos, estrategia, propuestas que descartas), con motivo y cifras. No se puede borrar.
- Compara el campeón (lo que opera el motor) con los aspirantes de la vigilancia. Una sustitución exige ≥ 40 operaciones nuevas, p < 0,05 corregida por Holm frente a su gemelo virtual, tus reglas y una confirmación posterior.
- Propone pausar el campeón si pierde con evidencia clara (≥ 30 operaciones y R contraído < −0,3).
- En modo «proponer» (por defecto) nada se aplica solo. Lo aplicado va a `estrategia_activa.json`, que el motor carga entre aperturas.
- Informe semanal cada lunes a las 7:00.
- **Potencia, siendo honestos:** una ventaja real de +0,27 R en 60 operaciones solo llega a p < 0,05 el 18 % de las veces. Una promoción tardará semanas o meses de paper.

**Desde la web** (WebSocket, solo desde la propia web del bot) puedes:
- silenciar, retirar, restaurar o añadir fuentes;
- fijar o deshacer el peso de un medio;
- aplicar, descartar o revertir propuestas;
- cambiar el modo de la evolución.

Todo queda en el historial.

## 16. Cómo se usa

- `abrir-radar.ps1` (acceso directo «Radar Fundamental»): arranca el bot si hace falta y abre la web en tus dos monitores.
- `python -m bot.backtest_aperturas --meses 24 --procesos 4`: vuelve a medir el motor de aperturas (unos 5 min).
- `python -m bot.backtest`: vuelve a medir las estrategias generales.
- `python -m bot.importar_fuentes <carpeta>`: añade fuentes verificadas.
- Interruptores (variables de entorno, `0` apaga):
  - `BOT_VIGILANCIA`, `BOT_APRENDIZAJE` y `BOT_EVOLUCION` apagan cada módulo;
  - `BOT_FUENTES_AUTO=0`: las reglas de fuentes solo sugieren;
  - `BOT_FUENTES_DESCUBRIR=0`: sin búsqueda de medios nuevos;
  - `BOT_EVOLUCION_MODO=proponer|automatico`: modo de la evolución (lo que elijas en la web manda).
- Los temporales del bot, también los de SQLite, van a `M:\BotFundamental-datos\tmp`.

## 17. Cartera de prueba de 100 € y funcionamiento sin nadie delante (28-10-2026)

- **Cartera de prueba:** `scripts/nueva_cartera_paper.py --eur 100` (con el bot parado) archiva la cartera anterior en `trades_archivo_<fecha>` y `paper_state_archivo_<fecha>`, sin borrar nada, y empieza una nueva de 100 € ficticios: 111,37 USDC al cambio EUR/USDC de Binance de ese momento. Su capital, guardado en `paper_state.cuenta`, manda sobre `BOT_PAPER_EQUITY`. Opera sola, en automático y con tus reglas: 0,5 % de riesgo por operación, como mucho 4 entradas y 2 % de riesgo al día, y una pausa con −3 % en el día. Resultados muestra el capital en USDC y su equivalente en euros.
- **Qué cabe esperar:** con 0,5 % de riesgo, 1 R es el 0,5 % de la cuenta. En el backtest fuera de muestra (variante de 7 días, sin validar) salieron +0,35 R por operación, unas 0,5 operaciones al día y 65 h de media por operación. Eso son unos +0,09 % al día, con rachas largas en negativo, no +2 % al día. Con la salida del laboratorio, +0,14 R. La cuenta honesta en avance del laboratorio perdió un 61 %.
- **Pestaña Backtest:** abre con la estrategia en marcha (E2, `GET /api/backtest/impulso`: curva de R acumulado dentro y fuera de muestra, salidas, largos y cortos, y las advertencias) y con el laboratorio. El motor v2 «ruptura» queda debajo, rotulado como anterior. Los datos salen de `BACKTEST_DIR/impulso_7d.json`, que escribe `controles/compara_impulso.py`.
- **Sin nadie delante:** el bot se puede arrancar suelto, sin ventana ni sesión de Claude. `scripts/vigilante-bot.ps1` comprueba `/health` cada minuto y lo vuelve a arrancar si cae; si escucha pero no contesta en 5 min, lo cierra con sus hijos antes. Registro en `logs\vigilante.log`; para pararlo, crea `M:\BotFundamental-datos\parar-vigilante`. El vigilante no se arranca solo con Windows: hace falta una tarea programada, que está pendiente de tu aprobación.
