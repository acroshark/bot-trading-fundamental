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

- **Cómo se calcula:** parte de la tasa base y se ajusta con factores visibles. Las de cripto (desde el 28-10-2026, sección 18): tasa base de la cripto en esa apertura, la reacción de la cripto en la apertura anterior (hoy pesa 0: no predice), su tendencia de 4 h, el flujo de órdenes, el minorista extremo y sus noticias. Ya no usan los cierres de los índices. Las de bolsa (probabilidad de que el índice cierre por encima de su apertura) quedan obsoletas: siguen en el mensaje, pero ni la web ni la skill radar las enseñan y el motor ya no las usa en su señal.
- **Noticias sin saturar:** antes se sumaban con un tope de ±10 puntos y una sola noticia bajista ya llegaba al tope; como el triaje sale bajista casi siempre (72 h: 1.697 bajistas y 313 alcistas), el 98 % de las predicciones eran bajistas (hallazgo n.º 3 de la auditoría). Ahora pesa la media ponderada por credibilidad, con una confianza que crece con el número de noticias, y se descuenta el tono medio de las noticias de 24 h. Las macro solo cuentan con reflejo confirmado en la cripto.
- **Cómo se mide:** todas se guardan y se comprueban, así que el acierto real queda a la vista. Al lado va `up_rate`, la proporción de veces que subió: si el acierto no supera a «siempre sube» o «siempre baja», no vale nada.
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

## 18. La cripto en cada apertura, el reflejo de las noticias y el prefiltro nuevo (28-10-2026)

Lo que pidió el usuario: no le importa cómo abre cada bolsa ni lo que hacen sus acciones, sino cómo reacciona la cripto
cuando abre cada bolsa, y las noticias de cripto. De las demás (guerras, sanciones, bancos centrales…), solo las que se ven
reflejadas en el mercado.

**Cifras medidas con las velas de 1 min** (`HIST_DIR/1m`, Binance al contado, 3.771 aperturas de las ventanas del motor del
1-11-2023 al 23-10-2026; scripts de referencia `oscilacion.py` y `encadenamiento.py`, reproducidas exactamente por
`bot/reaccion.py`):

| Rango (máximo − mínimo) ≥ 2 % tras la apertura | BTC | ETH | SOL | XRP | Alguna de las 4 |
|---|---|---|---|---|---|
| en las 4 h siguientes | 30 % | 50 % | 69 % | 51 % | 76 % |
| en las 8 h siguientes | 53 % | 77 % | 89 % | 75 % | 93 % |

- Moverse un 2 % no es ganar un 2 %: el rango cuenta la subida y la bajada, y nadie sabe de antemano por qué lado irá.
  La subida a las 4 h ronda el 50 % en todas (BTC 49,7 %, ETH 48,4 %, SOL 49,4 %, XRP 50,5 %).
- Por bolsa (3 años, hasta el 27-10-2026; `GET /api/cripto_aperturas`, `hist`): Nueva York y São Paulo son las que más
  mueven (rango ≥ 2 % a las 4 h en BTC el 51 %, SOL el 86 %); Europa y Dubái, las que menos (BTC 12 %).
- **Encadenamiento, sin mirar el futuro** (la siguiente apertura que empieza cuando ya se sabe lo que hizo la anterior en
  4 h): tras una apertura en positivo, la siguiente sube el **49,0 %** de las veces, frente al **50,1 %** de todas
  (15.076 casos, las cuatro monedas). Con lo que llevaba la anterior al abrir la siguiente: 48,5 % frente a 49,5 %.
  **No predice.**
- El **60 %** que sale al mirar «la siguiente apertura tal cual» es un artefacto del solape: São Paulo y Nueva York abren
  con entre 30 y 90 min de diferencia según la época del año (30 con EE. UU. en horario de verano, 90 desde el 1-11) y sus
  ventanas de 4 h se solapan, así que se mide casi dos veces el mismo movimiento. No es una señal y no se vende como tal.
- Con el periodo del módulo (los últimos 3 años, 28-10-2023 → 27-10-2026, 3.791 aperturas) salen las mismas cifras: 30, 50,
  69 y 51 % (alguna, 76 %) a 4 h; 52, 76, 88 y 75 % (alguna, 93 %) a 8 h; encadenamiento 49,0 % frente al 50,1 %.

**Mensaje `cripto_aperturas`** (`bot/reaccion.py`, contrato 2.14): cada minuto, por bolsa, el estado de su sesión (por abrir,
en curso las primeras 4 h, terminada), lo que lleva cada moneda desde la apertura (rendimiento, rango, máximo, mínimo y la
curva), la anterior completa y su historia de 3 años. La historia y el encadenamiento se calculan en un hilo al arrancar y
una vez al día (unos 50 s) y se guardan en `BACKTEST_DIR/reaccion_aperturas.json`.

**Predicciones:** las de cripto ya no usan los cierres de los índices («Hong Kong cerró −0,28 % → −6,3 pp» desaparece). En su
lugar, la reacción de la cripto en la apertura anterior, con el encadenamiento de arriba: como no predice, pesa 0 y la nota
lo dice. La tasa base de cada ventana sale de la historia de la cripto en esa apertura.

**Prefiltro** (`bot/filters.py`): tres clases.
- *cripto*: nombra una de las cuatro monedas (lleva esas, no las cuatro) o habla de cripto en general;
- *macro*: bancos centrales y tipos, inflación, aranceles, guerra, sanciones y, de empleo, PIB y banca, solo los datos de
  las grandes economías y las crisis financieras, más la geopolítica y la energía de gran impacto (estrechos y bloqueos,
  ensayos nucleares, ataques u ofensivas contra un país, golpes de Estado, pandemias, recortes de la OPEP y saltos del
  crudo de un 4 % o más), en 14 idiomas (`topics.FUERTE`). Va sin monedas y marcada macro;
- *contexto*: política, elecciones, empresas, tecnología, sucesos, energía, inmobiliario y economía local. Se capta para
  medir las fuentes, pero ya no va a la IA local ni lleva monedas. Antes era la mayoría de lo captado (temas de 1.532
  captadas: política 479, empresas 255, tecnología 187 y elecciones 164, frente a 140 de cripto), entraba con las cuatro
  monedas y saturaba la cola de la IA local (hallazgo n.º 2).

**Reflejo en la cripto** (`bot/reflejo.py`): una noticia que no es de cripto (por la clase del prefiltro, nunca por los
temas o el tipo que le ponga la IA local) y que el triaje dejó alcista o bajista solo cuenta si la cripto la confirma.
Desde su captura, con la media de las cuatro monedas (o BTC si falta alguna), y con un umbral que es el mayor de 0,5 % y
2σ del movimiento de 60 min de las 24 h anteriores (mediana de 0,93 %; del 0,52 al 1,48 % entre el 10 y el 90 % de los
casos):
- *confirmada*: se mueve al menos el umbral a favor de la noticia en los 60 min siguientes;
- *contraria*: al menos el umbral en contra;
- *sin reflejo*: pasados 60 min, ninguna de las dos;
- *pendiente*: mientras tanto. Manda lo que pase antes.
Solo las confirmadas cuentan en la señal del motor (y entonces valen para las cuatro monedas), en la balanza de la vista
Mundo, en el contexto del estratega y en las predicciones; en `/api/noticias` las demás salen con credibilidad ajustada 0.
Las de cripto cuentan como siempre.

**El azar (revisión del 28-10-2026).** Con momentos y direcciones al azar (6.000 casos, velas de 1 min de junio a octubre
de 2026, medidos con `reflejo.medir`):

| Umbral | Sale «confirmada» | Sale «contraria» |
|---|---|---|
| 0,5 % fijo (el primero) | 18,7 % | 18,6 % |
| mayor de 0,5 % y 2σ de 60 min (el de ahora) | 5,0 % | 5,2 % |

Con el 0,5 % fijo, casi una de cada cinco noticias se habría «confirmado» por puro azar, y en una hora de caída del 0,5 %
se confirmaban a la vez todas las bajistas (el triaje es bajista casi siempre): el factor de noticias se habría convertido
en impulso retrasado. Con las macro reales de 5 días (1.892 con dirección, 1.707 bajistas): con 2σ sale *confirmada* el
2,9 % y *contraria* el 5,1 %, y en las mismas horas con una dirección al azar, el 3,5 %. **La dirección del triaje no se
cumple más que al azar**: una confirmada es un filtro prudente, no la prueba de que la noticia moviera el precio, y la web
lo dice en el expediente de cada noticia. Cuánto vale como señal se mide en paper (`Reflejo.view()` lleva la tasa de
confirmación real y la del azar para compararlas).

**Cambio de régimen en el paper.** Este cambio altera lo que filtra la estrategia en marcha (news_mode «no_contra»): las
noticias de contexto salen de la señal (1.091 de 3.379 triadas con dirección en 24 h), la macro solo cuenta confirmada y
las predicciones de los índices dejan de pesar en la señal. La cartera de prueba de 100 € (desde el 28-10-2026) mezcla dos
regímenes desde el reinicio que lo active; queda apuntado en el historial de la evolución (`filtro_cambiado`,
`codigo-noticias-2026-10-28`). Para medirla limpia, empezar otra con `scripts/nueva_cartera_paper.py` al reiniciar.

## 19. ¿Noticias fiables + tendencia + entrada + salidas, con ≥ 6 operaciones al día? (28-10-2026)

El usuario pidió: elegir las noticias que más aciertan, confirmar la tendencia, entrar en cada apertura, poner los límites de
salida, sacar ≥ 1-2 % por apertura y hacer **como mínimo 6 operaciones al día**. Se midió cada paso. Los scripts y el
resultado completo están en `BACKTEST_DIR/controles/` (`acierto_noticias.py`, `seis_diarias.py` y `seis_diarias.json`).
Todo es solo lectura sobre `bot.db` y las velas de 1 min.

**1. Qué noticias aciertan más** (`news_outcomes`, del 23 al 28-10-2026: 13.888 historias×moneda con dirección, contada
una vez aunque la den 20 medios). El acierto es que la cripto vaya hacia donde decía la noticia a 1 h y a 4 h desde que el
bot pudo usarla, **ajustado al mercado**: se resta la deriva del día y la moneda, porque en una semana bajista toda noticia
bajista «acierta».

| grupo | n | acierto bruto 4 h | ajustado 4 h | movimiento ≥ 1 % a favor en 4 h |
|---|---|---|---|---|
| todas | 13.888 | 41,4 % | **49,4 %** | 2 % |
| alcistas / bajistas | 2.298 / 11.590 | 66,0 / 36,6 % | 50,8 / 49,1 % | 4 / 2 % |
| importancia ≥ 5 y \|dirección\| ≥ 40 | 8.453 | 42,7 % | 48,8 % | 2 % |
| verificadas | 742 | 46,6 % | 50,1 % | 3 % |
| medios T0 / T1 / T2 / T3 | 43 / 4.407 / 5.371 / 4.067 | — | 46,5 / 52,2 / 48,2 / 48,0 % | — |

Ningún tipo, nivel, importancia ni moneda se separa del azar con una muestra que valga. Los grupos que salen altos (energía
bajista 70 %, europapress.es 71 %, france24.com 90 %) tienen 50-200 historias de solo 3-4 días: con tan pocos días no se
distingue la noticia del día que hizo. Hacen falta semanas de paper para separar medios.

**2-4. Tendencia, entrada y salidas con 3 años de velas** (3.766 aperturas de las 5 ventanas del laboratorio, del 30-10-2023
al 20-10-2026; se elige con el 60 % más antiguo y se mide con el 40 % más reciente, desde el 13-08-2025).
- Entrada en el sentido de los primeros N min (5, 15 o 30), con un umbral de 0, 0,2 o 0,5 %.
- Tendencia: ninguna, la de la moneda en 24 h o la diaria de BTC.
- Bracket puro: stop de 1,5, 2 o 3 %, objetivo 2, 2,5 o 3R, con objetivo neto ≥ 3 %. Tope de 24 h o 7 días de seguridad.
- Una posición por moneda y 6 entradas al día como mucho (0,5 % × 6 = 3 %, el límite de pérdida diaria).
- Costes del laboratorio, sin funding.

En total, 432 variantes:
- Solo 6 llegan a unas 6 operaciones al día. Todas necesitan quitar todos los filtros, el stop de 1,5 % y el tope de 24 h;
  aun así se quedan en 5,2-5,7, porque la posición ocupa la moneda unas 11-13 h.
- **Las 6 pierden fuera de muestra: de −0,12 a −0,17 R por operación, de −0,32 a −0,44 % de la cuenta al día.**
- La mejor de cualquier frecuencia elegida en la muestra hace 1,3 operaciones al día y +0,036 R fuera de muestra.

**Qué acierto tendría que tener la noticia.** A cada apertura y moneda se le da el sentido «bueno» (el del precio 24 h
después, mirando el futuro) con probabilidad q. Mismas entradas a +15 min, salidas y límites (≤ 6 al día, 0,5 % de riesgo):

| acierto del sentido | stop 1,5 %, 2,5R | stop 1,5 %, 3R | stop 2 %, 2R | stop 3 %, 3R |
|---|---|---|---|---|
| 50 % (lo medido: 49 %) | −0,45 %/día | −0,43 | −0,30 | −0,14 |
| 60 % | +0,01 | +0,06 | +0,11 | +0,20 |
| 70 % | +0,48 | +0,55 | +0,52 | +0,53 |
| 80 % | +0,93 | +1,02 | +0,93 | +0,86 |
| 100 % (saber el futuro) | +1,86 | +1,96 | +1,73 | +1,50 |

Con las reglas del usuario y el riesgo de 0,5 % por operación, **ni sabiendo siempre hacia dónde irá el precio se llega al
+2 % diario de la cuenta**. Para no perder, la noticia tendría que acertar un 60 % de las veces hacia dónde estará el precio
24 h después de entrar (en bruto, desde la entrada de la apertura), y las medidas aciertan el 49 % (a 4 h desde el triaje,
ajustado al mercado). No son la misma medida: un grupo que acierte el 60 % a 4 h ajustado no deja la operación en tablas
(con la horquilla de 2,2R, R3 calcula que hace falta un 66 % a 4 h), y acertar el sentido no es cobrar la horquilla. Lo que falta no es la entrada ni las salidas: es una señal que acierte el sentido. Eso se sigue midiendo en
paper (aprendizaje por medio y el reflejo de la sección 18). Al reiniciar el 28-10, la cartera de prueba tenía 0
operaciones: el cambio de filtro de la sección 18 no mezcla regímenes en sus resultados.

## 20. Noticias fiables, entrada por noticia confirmada y estrategia de 6 diarias (28-10-2026)

El usuario pidió empezar por lo básico: si las noticias grabadas aciertan el 49 %, hay que quedarse con las que aciertan al
menos un 60 % de forma constante (no un día sí y otro no), aunque lleguen tarde; confirmar la tendencia; entrar y poner los
límites de siempre (≥ 2:1), y hacer ≥ 6 operaciones al día en la cartera de prueba de 100 €. Tres piezas nuevas, todo solo
paper (contratos 2.9, 2.15 y 2.16), revisadas el mismo día por una revisión estadística y otra de código.

**1. Noticias fiables (`bot/fiables.py`, estado `noticias_fiables`, `GET /api/noticias_fiables`).** Al arrancar y cada
hora, con lo que midió el aprendizaje (`news_outcomes`), sin mirar el futuro (solo días UTC completos y anteriores):
- Unidad: una historia×moneda (la del primer medio), con dirección y `acc240` (4 h desde que el bot pudo usarla: cuentan
  también las que llegan tarde). Acierto **ajustado al mercado**: dirección × (acc240 − media del día y la moneda) > 0.
  Una noticia que vale para las 4 monedas son 4 unidades casi iguales: cada grupo dice también sus **noticias distintas**.
- Grupos: medio, feed, tipo×sentido y medio×sentido. Un feed que es el único de su medio es el mismo grupo: no se prueba
  dos veces (antes, rbc.ru contaba como medio y como feed).
- **Criterios fijados de antemano**: ≥ 30 historias×moneda, ≥ 3 días con ≥ 5, acierto ≥ 60 %, cota inferior de Wilson al
  90 % ≥ 50 % y ≥ 50 % en cada uno de esos días. No se cambian mirando el resultado. Único cambio, fijado con 3 días de
  datos (hoy no cambia nada): desde 14 días de datos la constancia se mira por semanas. Por días el criterio pierde
  potencia con cada día que se suma:

  | acierto real del grupo | casos al día | pasa 30 días seguidos (por días) | pasa ~4 semanas (por semanas) |
  |---|---|---|---|
  | 60 % | 10 | 0,4 % | 86 % |
  | 60 % | 20 | 1,7 % | 97 % |
  | 70 % | 10 | 23 % | > 99 % |

- **Dos cuentas de azar** (500 permutaciones cada una): la fijada (dirección barajada dentro de cada moneda×día) y otra
  más exigente que **conserva la hora**: barajar el sentido de noticias enteras dentro de cada hora. La primera rompe que
  las 4 monedas de una noticia van juntas y que los aciertos se agrupan en el tiempo, así que sale demasiado estrecha. El
  veredicto se basa en el valor p de la segunda.
- **Puerta para operar** (añadida tras la revisión; antes de tener más datos). Un grupo «fiable» solo veta o abre entradas
  si además: el conjunto supera al azar que conserva la hora (p < 0,05); tiene ≥ 30 noticias distintas y una cota de
  Wilson ≥ 50 % contando un caso por bloque de 30 min; acierta ≥ 50 % en bruto (lo que cobra una operación sin cobertura)
  y la horquilla del usuario (1,5 %/4,5 % a 24 h, aproximada con el máximo a favor y en contra) da > 0 R en ≥ 30 noticias;
  y **hacia delante**, en los días posteriores a su primera elección y con las noticias que usaría en vivo (`senal` 1
  o 2: una macro cuenta aunque la cripto aún no la haya confirmado, porque quedarse con las confirmadas miraría el futuro;
  corregido en la revisión del 28-10-2026, versión 3), ≥ 30 noticias distintas con acierto ≥ 60 % y bruto ≥ 50 %. Cada
  grupo dice lo que le falta.
- Cada cálculo nuevo se añade a la tabla `noticias_fiables` (no se borra).

**2. Entrada por noticia confirmada (`bot/entrada_noticias.py`, eventos `entrada_noticia`).** Cuando se tría una noticia
de un grupo que pasa la puerta y nombra una moneda (o una macro que la cripto confirmó: las cuatro), se espera a que el
precio de esa moneda se mueva a su favor al menos el umbral del reflejo (el mayor de 0,5 % y 2σ de 60 min; sin σ todavía,
0,5 % provisional y se recalcula) en los 60 min siguientes al triaje. Si lo hace, entra por el mismo camino que el motor
(riesgo, aprobación y paper): stop 1,5 %, objetivo 3R (+4,5 %, ≥ 3 % neto), como mucho 24 h, largos al contado y cortos en
perpetuo, 0,5 % de riesgo. Si no, caduca; una confirmación vista tarde no se persigue. Respeta una posición por moneda y
los topes del día, **compartidos** con el motor (cuentan todas las entradas del paper del día de Madrid): los del preset
que tenga en marcha (6 entradas y 3 % con «6 diarias», 4 y 2 % con E2), nunca más de 6 y 3 %. Sin grupos que
pasen la puerta no entra nunca: las noticias esperan en la cola hasta que ya no se pueden confirmar a tiempo.
`BOT_ENTRADA_NOTICIAS=0` la apaga. Sus operaciones (`origen` `noticia_fiable`) no cuentan como del campeón E2 en la
evolución, y la web las separa en Resultados.

**3. Estrategia «6 diarias» (preset `seis` del motor de aperturas).** El impulso de los primeros 15 min sin filtros
(cualquier movimiento, sin volumen relativo, volatilidad ni BTC), entrada al medir, stop 1,5 % y objetivo 3R, como mucho
24 h, hasta 6 entradas y 3 % de riesgo al día, y como único filtro el veto de una noticia fiable (que pase la puerta) en
contra en las 6 h previas. No es una estrategia del laboratorio, así que no tiene gemelo en la vigilancia: se mide en
paper. El motor sigue con E2 hasta que se active con el bot parado: `scripts/activar_estrategia.py --preset seis` (y
`--preset impulso` para volver), que escribe `estrategia_activa.json` y lo apunta en el historial de la evolución como
«estrategia cambiada por decisión del usuario».

**4. Lo que dicen los datos.** Scripts y resultados en `BACKTEST_DIR/controles/`: `fiables_estudio.py` (R1),
`noticia_confirmacion.py` (R2), `historia_noticias_*.py` (R3) y `replica_seis.py` (R4). Todo de solo lectura.

*R1. ¿Hay noticias que acierten ≥ 60 % de forma constante?* Días completos: 25, 26 y 27 de octubre (el 23 no tiene noticias
con dirección y del 24 no hay datos). Son sesiones de 2, 5 y 3 h: 10 h de bot y unos 5 bloques de 4 h independientes.
Entran 6.597 historias×moneda, que son 1.763 noticias distintas.

| | lo medido | azar fijado (moneda×día) | azar que conserva la hora |
|---|---|---|---|
| grupos que pasan el criterio fijado | **6** (7 contando dos veces rbc.ru) | 3,4 de media (p = 0,11) | 3,8 de media, p95 7 (**p = 0,19**) |
| pasan también la puerta para operar | **0** | — | — |

La revisión probó un azar aún más exigente: cada día de noticias trasladado a un día de octubre sin noticias, a la misma
hora y con las direcciones y grupos reales (300 repeticiones). Con él pasan 8,4 grupos de media (p95 20) y P(≥ 7) = 0,54.
**Los grupos que pasan están en la mediana del azar bien hecho.** Por qué no operan:

| grupo | historias×moneda | noticias | ajustado | bruto | Wilson por bloques de 30 min |
|---|---|---|---|---|---|
| empresas ↑ | 128 | 32 | 81 % | 90 % | 48 % (9 bloques) |
| regulatorio ↓ | 289 | 80 | 64 % | 33 % | 37 % (17) |
| rbc.ru | 36 | 9 | 72 % | 50 % | 41 % (7) |
| geopolítica ↑ | 76 | 19 | 64 % | 55 % | 37 % (12) |
| política ↑ | 40 | 10 | 65 % | 65 % | 34 % (7) |
| ajunews.com ↓ | 32 | 9 | 66 % | 34 % | 39 % (7) |

- Muestra efectiva diminuta. El 90 % de las noticias cuentan ×4 monedas con el mismo resultado. Además, los aciertos se
  agrupan en el tiempo: hay una correlación de 0,5 dentro de bloques de 15 min. Por eso las 6.597 unidades valen unas
  50-250 observaciones independientes. empresas ↑ incluye unas 15 noticias de los resultados de SK Hynix en 15 min, y
  acierta solo el 32 % a 1 h.
- El ajuste por la media favorece a un sentido. El 27, cualquier noticia bajista «acertaba» el 64,5 %, y regulatorio ↓
  solo supera en 5,6 puntos a la base de su sentido. En bruto, lo que cobra una operación sin cobertura, regulatorio ↓ y
  ajunews ↓ aciertan un 33-34 %.
- Hacia delante, el 28 (día parcial, no usado para elegir), los 7 grupos juntos aciertan un 41,6 % ajustado y un 41,9 %
  bruto (976 casos). Es menos que el 43,1 % de todas las noticias de ese día:
  - rbc.ru, 9 %;
  - regulatorio ↓, 35 %;
  - ajunews ↓, 29 %;
  - solo geopolítica ↑ se mantiene (60-63 %), pero no supera al azar.
- Se mide sobre una población y se opera sobre otra. Casi todas las noticias de estos grupos son macro o no nombran
  moneda: en vivo solo valdrían si las confirma el reflejo, que existe desde el 28 a las 11:12. Solo regulatorio ↓ tiene
  casos que se operarían directamente (126 que nombran la moneda), y en ellos acierta el 50,0 %.
- «Aunque lleguen tarde»: el 97 % se capta en menos de 15 min. Con triaje a 15-60 min aciertan el 56,2 % (233 casos), por
  encima del azar fijado pero no de los exigentes. Con más de 60 min, el 58,5 % (82), por debajo de su azar. Cuando el
  precio ya se había movido a favor antes de captarla, el 49,7 %: llegar tarde con el precio movido es peor, no mejor.
- En cantidad darían para 6 operaciones al día de sobra: 21-75 noticias por sesión y 235 en las 8 primeras horas del 28.
  Lo que falla es el acierto: esas mismas noticias aciertan el 41,4 % el 28.

*R2. Noticia + confirmación del precio + horquilla* (19.133 historias×moneda del 25 al 28, con velas hasta el 28 a las
11:39 UTC).
- **Esos días no se podía ganar con las reglas.** El objetivo más pequeño permitido (3,75 %) solo se alcanzó en 24 h
  desde el 0,76 % de los minutos, frente al 23 % del resto de octubre. Hubo 0 objetivos en las 72 variantes, y ni un
  «oráculo» que conoce el sentido pasa de +0,27 R.
- La variante principal, fijada de antemano (reflejo en 60 min, stop 3 %, 3R), da −0,33 R por señal en 399 señales (el
  2,1 % de las noticias). Con la dirección barajada da −0,32 R (p = 0,98), y en cartera −0,25 %/día.
- **La variante implementada** (reflejo en 60 min, stop 1,5 %, 3R, 24 h) da −0,70 R por señal en esas 399 señales: 0
  objetivos y 41 % de stops. En cartera, −0,55 R. El 31 % de las operaciones acaba por fin de datos: hay que repetirlo con
  24 h completas después del 28.
- 0 de 72 variantes tienen R positivo, y frente a la dirección barajada la diferencia media es ≈ 0 R.
- Con 4 monedas, una posición por moneda y 24 h salen unas 2 operaciones al día, no 6.

*R3. 91 días de titulares públicos* (48.272 de 31 medios, del 29-07 al 27-10-2026; 8.178 casos con dirección).
- Acierto ajustado del 49,2 %. Ningún medio con más de 300 casos supera el 55,8 % en la cota superior del 90 %.
- El criterio por días no lo pasa ningún grupo (por azar, 0,11). Fuera de muestra no se elige nada, y el acierto de un
  medio en un tramo se correlaciona solo 0,24 con el del siguiente. Yahoo Finance pasó con los datos hasta el 13-08
  (68,7 %) y después acertó el 42,6 %.
- Con la horquilla 1,5 %/3,3 % (2,2R) salen −0,127 R por operación, y −0,145 R con la dirección al azar. Para no perder
  hay que acertar el sentido a 4 h un 66 %.
- Límites (revisión): el léxico coincide con el movimiento de las 24 h ANTERIORES en el 55,8 % de los casos (describe lo
  que ya pasó). Además son medios de cripto en inglés, así que no dice nada de los grupos que salieron en el bot.

*R4. «6 diarias» con el propio motor, 3 años* (`seis_diarias_resumen.json`, rehecho el 28-10 con el umbral 0 del preset y
las mismas cifras; corte el 16-08-2025 como en `impulso_7d.json`).
- Reproduce `seis_diarias.py` al decimal.
- Fuera de muestra: 1.538 operaciones a **−0,120 R** (−0,119 R con funding), con un 35 % de ganadoras. Salidas: 191 por
  objetivo, 865 por stop y 482 por tiempo. Duran 13,1 h de media y hay 5,0 por día de aperturas.
- Dentro de muestra: 2.536 operaciones a −0,111 R.
- Con 0,5 % de riesgo son unos −0,30 % de la cuenta al día. La cuenta simulada, con interés compuesto, pierde un 57 % en
  306 días. La exposición máxima del paper veta 964 intentos de entrada. **No hay ventaja.**

**Sobre «aunque solo acertemos un 50 %, con más de 2 a 1 se gana más de un 5 % al día».** Acertar el sentido no es cobrar
la horquilla: para cobrar 3R el precio tiene que recorrer +4,5 % antes que −1,5 %.
- Si se cobrara el objetivo en la mitad de las operaciones (no la mitad de los sentidos), con 6 al día y 0,5 % de riesgo,
  saldrían unos +3 %/día con 3R y +1,5 %/día con 2R, antes de costes.
- Con el sentido al azar, el objetivo del 4,5 % se cobró en el 13-17 % de las operaciones en 3 años, y en el 0 % del 25 al
  28 de octubre.
- Un +5 %/día con 0,5 % de riesgo son +10 R al día: 5 objetivos de 6 operaciones, todos los días. Con el riesgo fijado
  por las reglas, ni sabiendo el futuro se llega (sección 19: +1,96 %/día).

**5. Qué falta.**
- Hoy, con los datos que hay, **ninguna noticia acierta ≥ 60 % de forma constante**. El bot sigue midiendo cada hora y
  entrará solo si algún grupo pasa la puerta.
- La primera medida hacia delante llega cuando el 28 esté completo (el 29 a las 6:00 UTC). Después, cada día.
- Hace falta medir los grupos con la población que se opera en vivo (moneda nombrada o macro confirmada por el reflejo),
  que apenas tiene datos.
- Repetir R2 con 24 h completas después del 28.
- La cartera de prueba de 100 € (111,37 USDC, 0 operaciones al reiniciar) mezcla regímenes desde este reinicio: lo dice
  el historial (`codigo-entrada-noticias-2026-10-28`). Para medirla limpia, empezar otra con
  `scripts/nueva_cartera_paper.py`.
