# Auditoría de «qué falta» (28-10-2026)

Auditoría de solo lectura del sistema entero frente a lo que pidió el usuario. Cuatro auditores la hicieron con el bot en marcha:
en vivo, código, funcionamiento 24/7 y datos externos.
**Ningún hallazgo está verificado:** la fase de verificación adversarial no llegó a correr (se acabó el límite de uso).
Antes de arreglar nada, compruébalo en el código.

Ya resuelto el 28-10 en el PC del usuario:
- el bot ya no depende de la sesión de Claude: se arranca suelto y `scripts/vigilante-bot.ps1` lo relanza si cae (n.º 36, en parte 0, 37 y 46);
- la pestaña Backtest muestra la estrategia en marcha y el laboratorio;
- hay una cartera paper de prueba de 100 €.

Leyenda: severidad · origen · quién puede arreglarlo (código = cambio aquí; usuario = decisión, clave, cuenta o el otro PC).

## 0. El bot no está funcionando 24/7: en las últimas 72 h solo estuvo en marcha unas 6,3 h (un 9 %)
*alta · vivo · usuario · esfuerzo pequeño*

- **Evidencia:** bot.db (solo lectura), huecos de más de 5 min en events: 25-10 22:44→26-10 03:19 (275 min), 26-10 03:50→10:36 (406 min), 26-10 13:55→27-10 22:56 (1.981 min, 33 h) y 27-10 23:07→23:34 (27 min). Arranques en logs\bot.log: líneas 9382 (26-10 03:19), 11017 (26-10 10:36), 21934 (27-10 22:56) y 22797 (27-10 23:34). El proceso actual (PID 21160) cuelga de python 25512, que a su vez cuelga de Claude.exe (PID 6328). No hay tarea programada ni acceso directo en Inicio para el bot, y arrancar-bot.ps1 hace Wait-Process sin vigilante que lo reinicie. Consecuencias que se ven ahora mismo: GET /api/aperturas da saopaulo-2026-10-27 y nuevayork-2026-10-27 como «La sesión de esta apertura pasó con el bot parado: no se siguió», y faltan tokio-seul, china y europa del 27-10. En la tabla predictions hay 397 predicciones cripto con hit=-1 (sin evaluar), frente a 151 evaluadas. GET /api/aprendizaje da global.independientes=4 y dias=2. Solo hay 1 operación paper en 4 días.
- **Qué falta:** Un arranque que no dependa de la sesión de Claude: una tarea programada o un servicio que lo arranque con Windows y lo reinicie si cae. Además, una alerta visible en la web cuando el bot estuvo parado.
- **Arreglo propuesto:** Crear una tarea programada (al iniciar sesión, reiniciar si falla) que lance M:\BotFundamental-runtime\venv\Scripts\python.exe -X utf8 -m bot desde el proyecto. También que /health exponga el tiempo en marcha y el último hueco. Hace falta el visto bueno del usuario para tocar la configuración del sistema y dejar el PC encendido.

## 1. La IA local está saturada: la cola está siempre llena (120) y se quedan sin triaje cientos de noticias
*alta · vivo · código · esfuerzo medio*

- **Evidencia:** GET /health a las 23:55 da local_ai {queue:120, done:245} y a las 00:05 {queue:120, done:349}: unas 10,4 por minuto. local_ai.py:268 QUEUE_MAX = 120. En logs\bot.log, «Cola de la IA local llena» sube de 1 (23:36) a 341, 533, 747 y 896 noticias de menor prioridad sin triaje a las 23:56. En llama-server.log, las últimas 378 llamadas tardan 4,0 s de mediana (p90 4,8 s) con un solo hueco (-np 1); nvidia-smi da 6.971 de 8.192 MiB usados y la GPU al 92 %. En bot.db, desde el arranque de las 23:34 hay 1.420 capturadas y solo 302 triadas (11,5 por minuto). La latencia de captura a triaje es de 12 s de mediana pero de 164 s en el p90 y 970 s como máximo.
- **Qué falta:** Que la capacidad de triaje alcance el volumen captado (unas 3.300 por hora) o que lleguen menos noticias a la cola.
- **Arreglo propuesto:** Endurecer el prefiltro (ver el hallazgo siguiente), no mandar a la IA las noticias de contexto (fresh=false), y valorar -np 2 con un contexto menor o un modelo más pequeño solo para el triaje. Medir que la cola baje de 120.

## 3. Las predicciones son siempre bajistas porque el factor de noticias está saturado, y el «62 % de acierto» engaña
*alta · vivo · código · esfuerzo medio*

- **Evidencia:** GET /api/predicciones (23:50 y 00:00): los 17 items tienen prob_up < 0,5. Todos llevan el driver de noticias en −10 pp con la misma noticia de más peso («Rusia lanza más de 100 misiles balísticos contra Kiev…»), tanto para Tokio y São Paulo como para BTC o XRP. predict.py:37 NEWS_CAP_PP = 10.0 y predict.py:184 recorta la suma. En bot.db (solo lectura), predictions: 1.252 de 1.275 con prob_up ≤ 0,5, y el driver de noticias en el tope −10 en 1.082 de 1.237 casos. La dirección de lo triado en 72 h es negativa en 1.697, positiva en 313 y nula en 1.320. accuracy da hit_rate 0,6111 (00:00); con prob_up < 0,5 casi siempre, ese acierto solo es la proporción de veces que bajó el mercado (315 aciertos frente a 223 fallos, todos con prob_up < 0,5).
- **Qué falta:** Un factor de noticias que no se sature con el volumen ni con la geopolítica general, y un acierto comparado con una base ingenua («siempre baja»).
- **Arreglo propuesto:** Normalizar la presión de noticias por número de noticias y relevancia para cada destino (bolsa o moneda), descontar el sesgo medio diario y mostrar en la web el acierto frente a la tasa base de «siempre baja» y «siempre sube». Sin consejo de inversión: es calidad de la medida.

## 14. La fábrica de contenidos queda bloqueada mientras el llama-server del bot (alias qwen-trading) esté encendido, y eso es casi siempre
*alta · codigo · usuario · esfuerzo medio*

- **Evidencia:** M:\OneDrive\herramientas\OpenClaw\fabrica\lib\gpu.py:51-52 (ALIAS_OPENCLAW = "qwen3.5-9b") y :374-378 (cualquier llama-server sin ese alias se trata como ajeno y lanza GPUOcupada("otro_llm_abierto")). TurnoGPU.__enter__ (:240-246) crea gpu.lock y en ese mismo instante llama a _comprobar_llm(matar=False). El bot solo mira gpu.lock cada 30 s (bot/local_ai.py:721-735) o antes de cada petición (:508), así que no llega a ceder la GPU. Las automatizaciones usan otra copia del alias: fabrica\lib\vigilancia.py:48 y :701-730, y fabrica\lib\automatizaciones.py:1318-1319 esperan con «otro_llm/hay_otro_llama_server» sin crear gpu.lock, de modo que el bot nunca se entera. Estado real (28-10): un solo llama-server, PID 8456, con alias qwen-trading en el puerto 8094, encendido desde las 23:34. bot.log no registra ningún «IA local apagada» desde el 26-10 13:49. /health da local_ai.queue = 120, es decir, la cola está llena y nunca llega a estar 15 min sin uso.
- **Qué falta:** El efecto real es que generar_imagen.py, crear_video.py y animar_imagen.py fallan siempre con gpu_ocupada:otro_llm_abierto, y las automatizaciones de la fábrica no arrancan mientras el bot esté en marcha (24/7). El mecanismo de «ceder la GPU con gpu.lock» descrito en docs/03 y en local_ai.py:3-4 no llega a funcionar. Lo bueno es que la fábrica nunca mata el servidor del bot.
- **Arreglo propuesto:** En la fábrica, gpu.py y lib/vigilancia.py deberían tratar qwen-trading como un servidor que cede: tras tomar gpu.lock, esperar hasta unos 60 s a que desaparezca (el bot lo apaga al ver el lock si no está generando) antes de dar otro_llm_abierto; las automatizaciones no deberían contarlo como ajeno, o deberían crear el lock y esperar. Hay que hacer el cambio en los dos archivos, porque cada uno tiene su propia constante. Alternativa: que la fábrica lo mate por PID igual que el suyo. Lo decide el usuario (es otro proyecto y la memoria lo marca como pendiente de su decisión).

## 15. Los post-mortems de las operaciones nunca se escriben porque la cola de triaje de la IA local está siempre llena
*alta · codigo · código · esfuerzo pequeño*

- **Evidencia:** bot/aprendizaje.py:1448-1455: local_generator solo genera si ai.q.empty(). bot/local_ai.py:268 tiene QUEUE_MAX = 120, y GET /health devolvió local_ai.queue = 120 en tres lecturas seguidas. En bot.db (solo lectura), postmortems tiene 1 fila con estado 'pendiente' (origen paper, cerrada=1793007404506, unas 37 h antes) y GET /api/aprendizaje da totales.postmortems = {hechos: 0, pendientes: 1}. bot.log tiene 3.340 líneas de «Cola de la IA local llena».
- **Qué falta:** El usuario pidió saber «por qué funcionaban, por qué no»; con 5.714 fuentes la IA local nunca tiene hueco y la explicación de cada operación se queda pendiente para siempre.
- **Arreglo propuesto:** Reservar hueco para los post-mortems: por ejemplo, uno cada N triajes, o cuando el más antiguo pendiente pase de X horas, o cuando en la cola solo queden noticias de baja prioridad (silenciadas o de muestreo). Otra opción es generarlo durante el briefing. Y añadir una prueba con la cola llena.

## 36. El bot vive dentro de la sesión de Claude Desktop
*alta · 24x7 · código · esfuerzo pequeño*

- **Evidencia:** Proceso 21160 «python -X utf8 -m bot» → padre 25512 (lanzador del venv) → padre 6328 Claude.exe (Win32_Process). preview_list: servidor «bot» (de .claude/launch.json:4-8), startedAt 2026-10-27T22:34:36Z, ligado a la sesión local_d2fcd39e. bot.log: cada ejecución termina sin la línea «El bot se detuvo…» de __main__.py:197; p. ej. la última línea del 26-10 es 13:55:27 «Web desconectada» y la siguiente es el arranque del 27-10 22:56. Se mató de golpe, sin pasar por el finally de __main__.py:199-213.
- **Qué falta:** Que el bot funcione sin Claude abierto. Si se cierra o se actualiza Claude Desktop, o termina esta sesión, el bot muere, y con él las aperturas, la vigilancia y el aprendizaje.
- **Arreglo propuesto:** Dejar la vista previa de Claude solo para desarrollo. Escribir un lanzador propio (vigilar-bot.ps1 con pythonw, sin ventana) que arranque «python -X utf8 -m bot» y lo vuelva a lanzar si sale, esperando 15-30 s y con un tope de reintentos por hora. Registrar sus reinicios en M:\BotFundamental-datos\logs. Apagado limpio con Ctrl+Break o un fichero de parada, para que corran los close().

## 37. Nada lo rearranca: ni si se cae, ni al iniciar Windows, ni tras un reinicio
*alta · 24x7 · usuario · esfuerzo medio*

- **Evidencia:** No hay tarea programada del bot. Get-ScheduledTask solo muestra OpenClaw Gateway y las 2 rutinas de Pr3cise. Las claves Run de HKCU y HKLM no tienen nada del bot. En la carpeta Startup solo están «Claude - Agente Prospector», «Journi guide imagenes» y «OpenClaw vigilante». No hay servicio ni nssm/winsw. AutoAdminLogon está vacío. Windows Update: horas activas de 8 a 17 (HKLM\...\WindowsUpdate\UX\Settings ActiveHoursStart=8, End=17; UxOption=0), así que puede reiniciar de noche, cuando abren Asia y Europa. PendingFileRenameOperations=True. Kernel-Power 41: 6 apagados inesperados en 60 días (01-10, 07-10, 09-10 con BSOD 0x133, 11-10, 15-10 y 18-10), sin SAI (Win32_Battery vacío). HiberbootEnabled=1. abrir-radar.ps1:19 lanza el bot en una consola minimizada (si se cierra, el bot muere) y arrancar-bot.ps1:4-7 lo ata a la ventana de PowerShell.
- **Qué falta:** Un arranque automático al encender o reiniciar el PC y un vigilante que lo relance si cae. Hoy, tras cualquier reinicio o cuelgue, el bot sigue parado hasta que alguien pulse el acceso directo.
- **Arreglo propuesto:** Con permiso del usuario, crear una tarea programada «Radar Fundamental - bot»: al iniciar sesión, o al iniciar el sistema aunque no haya sesión, que ejecute el lanzador anterior, con «reiniciar si falla cada 1 min, hasta 999 veces» y sin límite de duración. Para que arranque sin nadie delante, el usuario activa el inicio de sesión automático (Autologon de Sysinternals; la contraseña la teclea él). Ampliar las horas activas a 18 h y pausar las actualizaciones en semanas de prueba. Valorar un SAI, porque hubo 6 cortes en 60 días.

## 38. El PC se apaga cada noche y se pierden 8 de las 10 aperturas
*alta · 24x7 · usuario · esfuerzo pequeño*

- **Evidencia:** Sistema: el 26-10 a las 23:50:47 hay un 1074 (Apagar, iniciado por RuntimeBroker) y un 42 (entra en suspensión), y no vuelve hasta el 27-10 a las 12:18:34 (Power-Troubleshooter 1). Lo mismo el 14-10 a las 23:22, el 19-10 a las 09:53 y el 20-10 a las 00:21. powercfg: con corriente, «Suspender tras» y «Hibernar tras» valen 0 (nunca); la suspensión no es automática, la provoca el usuario o el inicio rápido. bot.db solo tiene eventos en 14 horas de los últimos 5 días, con un hueco del 26-10 14:00 al 27-10 22:00. Aperturas en hora de Madrid (regions.py:25-34, horario de invierno): Tokio y Seúl a las 01:00, Shanghái y Hong Kong a las 02:30, Dubái a las 07:00, Londres, Fráncfort y Madrid a las 09:00, São Paulo a las 14:00 y Nueva York a las 14:30 (15:30 desde el 1-11). Con el PC apagado de 23:50 a 12:18 se pierden todas salvo São Paulo y Nueva York. El bot no pide que el equipo siga despierto: no hay SetThreadExecutionState en bot/*.py.
- **Qué falta:** Que el equipo siga encendido de noche. Sin eso no hay 24/7 en este PC, y Asia, que es donde arranca la cadena de aperturas que quiere seguir el usuario, nunca se mide.
- **Arreglo propuesto:** Decisión del usuario: dejar este PC encendido (no usar «Apagar» ni «Suspender») hasta que esté listo el PC dedicado. Si lo quiere, se puede poner el botón de encendido en «No hacer nada». En el bot: llamar a SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED) mientras corre, para que Windows no suspenda por inactividad aunque cambie el plan, y anotar en la web los huecos de cobertura («apagado de X a Y») para que no se confundan con días sin señales.

## 39. bot.db no tiene ninguna copia de seguridad
*alta · 24x7 · código · esfuerzo pequeño*

- **Evidencia:** La única base es M:\BotFundamental-datos\bot.db: 266 MB, más 34,3 MB de WAL. Fuera de las carpetas temporales de las pruebas no hay *.db, *.bak ni *.sqlite. M:\Backups solo tiene «copia-claude» del 11-10. El Historial de archivos (fhsvc) está parado y en manual. Los datos están fuera de OneDrive a propósito (config.py:1-6), así que tampoco hay copia en la nube. Todo está en un único SSD (CT2000MX500) que ya sufrió 6 apagados bruscos en 60 días. En la base está lo que el usuario pidió conservar: evolution_log (solo se añade), trades, medios_*, news_outcomes, predictions y paper_state.
- **Qué falta:** Una copia automática diaria, hecha en caliente, de la base que guarda el historial del sistema que aprende.
- **Arreglo propuesto:** Tarea diaria, o dentro de maintenance() una vez al día, que use la API de copia de sqlite3 (src.backup(dst)) o «VACUUM INTO» hacia M:\BotFundamental-datos\copias\bot-AAAA-MM-DD.db. Rotar 7 diarias y 4 semanales. Copiar la semanal comprimida a D: (29 GB libres) o a OneDrive, nunca la base viva. Comprobar cada copia con PRAGMA integrity_check.

## 40. El 24/7 está sin probar: nunca ha corrido más de unas 3,5 h seguidas
*alta · 24x7 · código · esfuerzo medio*

- **Evidencia:** Eventos por hora en bot.db (abierta en solo lectura): solo hay datos el 23-10 de 03 a 04 y de 15 a 18, el 25-10 de 21 a 22, el 26-10 a las 03 y de 10 a 13, y el 27-10 de 22 a 23. El tramo continuo más largo va del 26-10 10:36 a las 13:55. La ejecución actual empezó el 27-10 a las 23:34. Ahora el proceso usa 546 MB de memoria activa, 706 MB privados y 1.793 handles. Falta saber si eso crece en 24-72 h, igual que la base, la cola de la IA y el tamaño del WAL.
- **Qué falta:** Una prueba larga, de al menos 72 h sin nadie delante, con mediciones de memoria, handles, retraso del bucle, tamaño de la base y del WAL, cola de la IA local y reconexiones.
- **Arreglo propuesto:** Cuando exista el lanzador con rearranque: una tarea de solo lectura que cada 5 min apunte en M:\BotFundamental-datos\logs\salud.csv el resultado de GET /health, la memoria y los handles del proceso, y el tamaño de bot.db y del WAL. Revisar a las 24 h y a las 72 h. Criterio: sin crecimiento sostenido de memoria, sin reinicios y con cobertura de las 10 aperturas.

## 56. La comisión de 0,10 % solo vale con cuenta X-Perps; sin ella el taker es 0,35 %
*alta · externo · código · esfuerzo pequeño*

- **Evidencia:** config.py:63 usa FEE_PCT = 0.10 «(OKX EU, base)» y docs/01-viabilidad.md:93 dice «Comisiones de 0,08/0,10 %». Las tarifas oficiales de OKX para el EEE desde el 22-04-2026 son 0,20 % maker y 0,35 % taker en el nivel Regular (okx.com/help/notice-updates-to-okx-eea-trading-fees, 08-04-2026). La tabla de 0,08 % y 0,10 % es solo para cuentas X-Perps (okx.com/help/eea-xperp-spot-trading-fees-comparison, modificada el 19-10-2026). El paper y el motor operan a mercado, es decir, como taker.
- **Qué falta:** El coste depende de que el usuario tenga la cuenta X-Perps desbloqueada, y eso no se dice en ninguna parte. Sin ella, el coste de ida y vuelta es de unos 0,74 % más deslizamiento, frente al 0,24 % supuesto (paper.py:169). Eso cambia la regla de 3 % neto (apertura.py:1247), la puerta de 3 × coste y todos los backtests.
- **Arreglo propuesto:** Hacer FEE_PCT configurable (por ejemplo BOT_FEE_PCT, 0,10 con cuenta X-Perps y 0,35 sin ella), añadir al laboratorio y a backtest_aperturas una sensibilidad a 0,35 % y corregir docs/01 y docs/05 con la condición. Después, el usuario confirma en su cuenta qué tarifa le aparece en BTC-USDC.

## 2. El prefiltro deja pasar política, empresas y tecnología genéricas y les asigna BTC, ETH, SOL y XRP
*media · vivo · código · esfuerzo medio*

- **Evidencia:** filters.py:89-92: con cualquier tema «relevante», la noticia pasa con list(config.COINS). Desde el arranque, los temas de las 1.532 capturadas son politica 479, empresas 255, tecnologia 187 y elecciones 164, frente a solo 140 de cripto. Ejemplos de «Relevante» en bot.log: 23:57:32 «Qué aprendió Jeff Bezos trabajando como cocinero…» (tema empresas, 4 monedas), 23:57:27 «Maryland election's board confirms DOJ election monitors» y 23:57:33 «苹果最贵笔记本电脑…MacBook Pro…» (tema macro). La IA puntúa con importancia 0-2 a 144 de 333 triadas desde el arranque (43 %). GET /api/estado a las 00:05 dice que la IA local está «Clasificando «18억 아파트 8억에 또 나왔다…»», una noticia de pisos en Corea.
- **Qué falta:** Un prefiltro que separe lo que puede mover cripto o las bolsas vigiladas del ruido general, y que no reparta las 4 monedas a todo.
- **Arreglo propuesto:** Exigir tema fuerte (STRONG_TOPICS) o mención de moneda para entrar en la cola. Lo genérico iría a contexto, sin IA, o en muestreo. Reasignar monedas solo cuando el titular las mencione.

## 4. El post-mortem de la única operación paper está pendiente desde el 26-10 y no se escribirá mientras la cola siga llena
*media · vivo · código · esfuerzo pequeño*

- **Evidencia:** GET /api/aprendizaje/postmortems: p:t5578130ca6ae con estado "pendiente", texto null y modelo null. En bot.db, postmortems tiene intentos=0 y totales.postmortems {hechos:0, pendientes:1}. aprendizaje.py:1453 solo genera si `ai.q.empty()`, y la cola está siempre en 120 (GET /health).
- **Qué falta:** Una reserva de capacidad para los post-mortems (son pocos), aunque haya triaje esperando.
- **Arreglo propuesto:** Permitir un post-mortem cada N minutos aunque la cola no esté vacía, o meterlo en la cola con prioridad alta.

## 5. Fuentes: el 40 % de los feeds nunca trajo nada; China y Hong Kong no llegan a 200 feeds productivos y nada se silencia solo
*media · vivo · código · esfuerzo medio*

- **Evidencia:** Configuradas, todas pasan de 200 (GET /health: jp 382, kr 523, cn 433, hk 414, me 567, de 855, uk 650, es 431, us 849, latam 610). Pero, cruzando /api/fuentes con medios_conteo (clase «fuente», desde el 23-10), 2.266 de 5.714 feeds nunca dieron ni una noticia captada ni filtrada. Con al menos una: cn 192, hk 187, jp 231, uk 323. Fallan 115 (23:55) y 135 (00:05); los errores principales por región son cn HTTP 15, uk HTTP 16, de «Respuesta sin RSS ni Atom» 13 y us 7, y hay 53 con HTTP 403. 73 feeds no han funcionado nunca (last_ok vacío). El silencio por caída técnica no llega a activarse: fuentes_vivas.py:130 TEC_HORAS = 72 y _salud (línea 868) solo suma horas entre revisiones horarias con el bot en marcha. En bot.db, source_health tiene 44 filas, todas con horas 0.0. GET /api/fuentes_vivas da feeds {activa:5714, silenciada:0, retirada:0} y revision.medidas 0.
- **Qué falta:** Retirar o reponer los feeds muertos y garantizar al menos 200 feeds productivos en cn y hk. La caída técnica debería contarse aunque el bot se reinicie.
- **Arreglo propuesto:** Calcular la caída técnica con feed_state.last_ok y fails (tiempo real desde el último éxito) en lugar de horas acumuladas en marcha. Hacer una pasada para retirar los 73 que nunca funcionaron y los 403, y añadir fuentes directas para China y Hong Kong.

## 6. Las noticias de China y Hong Kong llegan con 8-9 horas de retraso de mediana
*media · vivo · código · esfuerzo grande*

- **Evidencia:** GET /health, feeds.regions.median_lat_s: cn 33.829 s (9,4 h), hk 30.112 s (8,4 h) y jp 4.746 s (1,3 h), frente a kr 421 s y latam 373 s. En /api/fuentes hay 181 feeds con latencia mediana de más de 6 h, sobre todo búsquedas de Google News (por ejemplo «Google News CN - 人民银行 货币政策» con 82.124 s y «GNews HK EN Hong Kong property» con 85.907 s). Son justo las aperturas que opera el motor (tokio-seul y china, GET /api/aperturas params.windows).
- **Qué falta:** Fuentes directas y rápidas (RSS de los propios medios o agencias) para Asia en lugar de búsquedas de Google News.
- **Arreglo propuesto:** Ordenar por median_lat_s, dar de baja o bajar de prioridad los feeds GN con más de 6 h y buscar RSS directos de Xinhua, Caixin, SCMP, RTHK, Nikkei, etc.

## 7. La fase 2 no tiene clave: verificador, impacto y estratega están apagados
*media · vivo · usuario · esfuerzo pequeño*

- **Evidencia:** GET /api/fase2 da {claude:false, estado:"nokey", presupuesto {limite_usd:3, gastado_usd:0}, planes:[]}. En el snapshot del WS, los agentes verifier, impact y strat aparecen con off:true y la nota «Falta la clave ANTHROPIC_API_KEY: uso el respaldo local». ANTHROPIC_API_KEY no existe ni en el proceso, ni en el usuario, ni en la máquina, y no hay .env. La tabla claude_spend está vacía.
- **Qué falta:** La clave de la API (decisión del 22-10: API con créditos, nunca la suscripción), o asumir que la fase 2 funciona solo en local.
- **Arreglo propuesto:** El usuario decide si añade ANTHROPIC_API_KEY como variable de entorno del usuario (no en archivos del proyecto) y reinicia el bot. Si no, la web debería dejar claro que funciona en modo local.

## 16. La web no enseña los post-mortems ni la explicación de cada operación cerrada
*media · codigo · código · esfuerzo medio*

- **Evidencia:** dashboard/radar.html:2605: onApr guarda S.apr.postmortems y S.apr.cambios, pero ninguna función los pinta (no aparecen en ningún render). Solo salen dentro de newsItem (radar.html:2551), y únicamente si la noticia está entre las 6 mejores o peores a 4 h. Las rutas /api/aprendizaje/postmortems y /api/aprendizaje/cambios no las usan ni la web ni radar.py. Cada post-mortem tiene un `resumen` sin IA que tampoco se ve.
- **Qué falta:** Ver en Resultados o en Evolución, por cada operación paper o virtual, qué noticias y medios aportaron (tabla aportaciones) y por qué ganó o perdió.
- **Arreglo propuesto:** Una lista de post-mortems en la pestaña Evolución, o en cada fila de Resultados, con resumen, texto (si existe) y estado.

## 17. La skill radar de OpenClaw y su instalador no conocen aprendizaje, evolución, fuentes vivas ni vigilancia, y describen un motor que ya no opera
*media · codigo · código · esfuerzo medio*

- **Evidencia:** openclaw-trading/workspace/skills/radar/scripts/radar.py solo llama a /api/estado, noticias, tendencia, predicciones, backtest, aperturas y cartera. :162 dice «… → stop dinámico → reentradas», que es el modo v2 y contradice las reglas del usuario, y no muestra paused, params_source, estrategia_cargada, plan ni instrumento. :135 lee a.get("backtest").best de /api/aperturas, pero openings ya no trae «backtest», así que ese bloque nunca imprime nada (los datos están en /api/backtest/aperturas). instalar.py:77-78: la lista de permitidos solo admite estado|flujo|agenda|balanza|tendencia|predicciones|backtest|cartera|aperturas. SKILL.md y AGENTS.md listan lo mismo. La copia instalada en M:\BotFundamental-openclaw\workspace es idéntica byte a byte y es del 25-10, anterior a los módulos del 27-10.
- **Qué falta:** Comandos como evolucion (campeón, propuestas, informe), aprendizaje (ranking de medios, estadísticas por región y ventana, post-mortems), fuentes (silenciadas y candidatos) y vigilancia (E1, E2, E1-7d, E2-7d en vivo frente al backtest), con la descripción del motor impulso al día.
- **Arreglo propuesto:** Añadir esas funciones en radar.py (solo GET), ampliar la expresión de instalar.py:77, poner al día SKILL.md y AGENTS.md, corregir el texto de aperturas() y apuntar backtest() a /api/backtest/aperturas. Después ejecutar instalar.py y arrancar-openclaw.ps1 para reaplicar los permisos.

## 18. La balanza de radar.py cuenta con credibilidad completa las noticias de fuentes silenciadas
*media · codigo · código · esfuerzo pequeño*

- **Evidencia:** openclaw-trading/workspace/skills/radar/scripts/radar.py:72 usa (x.get("credibilidad_ajustada") or x.get("credibilidad") or 60). /api/noticias (bot/server.py:146-149, contrato 3) da credibilidad_ajustada = 0 para una fuente silenciada, y como 0 es falso en Python se usa la credibilidad sin ajustar. Tampoco se mira estado_fuente.
- **Qué falta:** Que la lectura «hacia dónde empujan las noticias» del analista de OpenClaw excluya lo que el motor excluye.
- **Arreglo propuesto:** Usar credibilidad_ajustada cuando no sea None (aunque valga 0) y saltar las noticias con estado_fuente silenciada o retirada.

## 19. La balanza de noticias y la recomendación de la vista Mundo ignoran el peso de la fuente y el peso aprendido del medio
*media · codigo · código · esfuerzo medio*

- **Evidencia:** dashboard/radar.html:3268-3282: newsTilt pondera magnitud × credibilidad × importancia × decaimiento × confirmaciones, sin srcWeight ni peso del medio. En toda la web no aparece srcState ni srcWeight. Contrato docs/04-contratos.md §2.9 («Señal de noticias … × srcWeight; la credibilidad sale multiplicada por el peso aprendido de su medio») y §2.6 (las silenciadas no cuentan).
- **Qué falta:** Las «noticias que inclinan la balanza» y el «qué operar y si es buen momento» de la vista Mundo (radar.html:3313-3348) pueden contar medios silenciados o de peso 0 que el motor descarta. Hoy apenas se nota (1 medio silenciado y todos los pesos en 1), pero irá divergiendo a medida que aprenda.
- **Arreglo propuesto:** Leer srcWeight del patch de captured, corroborated y srcstate en el expediente y multiplicarlo en newsTilt. El peso del medio puede salir de S.apr o, mejor, publicarse en el patch del triaje. Marcar visualmente las noticias de fuentes silenciadas o en prueba.

## 21. Las estadísticas de acierto por nivel, región, idioma, tipo, ventana, fase, rol y moneda no se ven en ninguna parte
*media · codigo · código · esfuerzo medio*

- **Evidencia:** GET /api/aprendizaje publica por_nivel, por_region, por_idioma, por_tipo, por_ventana, por_fase, por_rol y por_moneda, y ninguno de esos nombres aparece en radar.html. Nadie consume la ruta /api/aprendizaje/estadisticas (bot/server.py:668), ni la web ni radar.py. docs/05 §15 y el docstring de aprendizaje.py prometen «el acierto de cada medio, región, idioma, tipo de noticia y ventana».
- **Qué falta:** El usuario pidió ver «en qué momento se decían» y «por qué funcionaban»; el bot ya lo calcula, pero no se enseña.
- **Arreglo propuesto:** Una tabla en Evolución › Medios con un selector de dimensión, leyendo /api/aprendizaje/estadisticas?por=… y mostrando casos independientes e IC para no sobreinterpretar.

## 23. El paper no exige la regla del usuario «objetivo ≥ 3 % neto»: solo stop ≥ 1,5 % y ≥ 2R
*media · codigo · código · esfuerzo pequeño*

- **Evidencia:** El contrato docs/04-contratos.md:29 define las reglas como «stop ≥ 1,5 %, objetivo ≥ 2R y ≥ 3 % neto». bot/paper.py:684-690 (_invalid) solo comprueba el stop y el R:R. bot/claude_agents.py:472-476 (make_ticket) tampoco calcula el neto. Con stop 1,5 % y 2R, el objetivo bruto es 3 % y el neto queda por debajo (contrato §2.8: 2,76 %). El motor impulso sí lo exige (apertura.py:1596), y también la evolución (MIN_NET_PCT).
- **Qué falta:** Hoy no pasa porque la fase 2 está sin ANTHROPIC_API_KEY (comprobado: no existe) y el estratega por reglas usa 3R. En cuanto Opus proponga 1,5 % × 2R, el paper lo aceptaría.
- **Arreglo propuesto:** Añadir un RULE_MIN_NET_PCT = 3.0 en config y comprobarlo en paper._invalid con cost_rt_pct, que el paper ya calcula, más una prueba con USER_RULES activado.

## 31. No hay arranque automático ni vigilante para el funcionamiento 24/7
*media · codigo · usuario · esfuerzo pequeño*

- **Evidencia:** Get-ScheduledTask solo muestra «OpenClaw Gateway», que es de la fábrica. No hay ninguna tarea del bot ni del OpenClaw de trading. arrancar-bot.ps1 hace Start-Process y Wait-Process, sin reinicio si se cae. docs/03 §4.5 lo deja como pendiente. GET /api/aperturas muestra «La sesión de esta apertura pasó con el bot parado: no se siguió» en São Paulo y Nueva York del 27-10.
- **Qué falta:** Que el bot y la vigilancia no pierdan aperturas por un reinicio de Windows o una caída.
- **Arreglo propuesto:** Una tarea programada al iniciar sesión, o un servicio, que ejecute el bot con reinicio al fallar (y otra para el OpenClaw de trading). Es configuración persistente del sistema: necesita el permiso explícito del usuario.

## 41. Si el bot se cae nadie se entera
*media · 24x7 · usuario · esfuerzo pequeño*

- **Evidencia:** En bot/*.py no hay ningún canal de aviso: buscar telegram, ntfy, toast, smtp o webhook solo da una mención en fuentes_vivas.py y otra en un JSON de fuentes. docs/03-mundo-ia-local-openclaw.md:93 deja pendiente Telegram, porque el token de @BotFather solo lo puede crear el usuario. El estado solo se ve abriendo la web en http://127.0.0.1:8765.
- **Qué falta:** Un aviso al móvil cuando el bot cae, se reinicia, se queda sin precios o fuentes más de X minutos, o se salta una apertura.
- **Arreglo propuesto:** El usuario crea el bot en @BotFather y guarda el token en una variable de entorno, sin pasarlo por el chat. Después, el vigilante (no el bot) manda un aviso cuando /health falla dos veces seguidas, cuando relanza el proceso, y cada mañana con un resumen de cobertura (aperturas vistas o perdidas).

## 42. IA local saturada y GPU al 92 %, a 86-87 °C y con freno térmico
*media · 24x7 · código · esfuerzo medio*

- **Evidencia:** nvidia-smi (28-10, 00:0x): RTX 3060 Ti al 92 % de uso, 86-87 °C, 172-177 W, ventilador al 100 % y clocks_event_reasons.sw_thermal_slowdown activo. VRAM: 6.921 de 8.192 MiB usados, 1.104 libres. GET /health: local_ai.queue entre 119 y 120 (la cola está llena) y 1.590 noticias relevantes en la última hora. bot.log a las 00:01:47: «Cola de la IA local llena: 1026 noticias … sin triaje», en 27 minutos de ejecución. llama-server.log: unos 6 s por triaje (total 5975 ms con 354 tokens), o sea unas 10 por minuto, frente a unas 26 por minuto que llegan. El apagado por inactividad (LOCAL_AI_IDLE_S=900, local_ai.py:717-719) exige que la cola esté vacía, así que nunca ocurre. llama-server lo lanza el propio bot (PID 8456, hijo de 21160; local_ai.py:535-543; -c 32768, alias qwen-trading, puerto 8094).
- **Qué falta:** Que el triaje dé abasto sin tener la GPU al límite las 24 h. Hoy más de la mitad de las noticias relevantes se quedan sin nota.
- **Arreglo propuesto:** Filtrar antes de la IA: solo noticias con moneda, macro o bolsa detectada y prioridad mínima, con un tope por minuto y prioridad para la región de la próxima apertura. Bajar max_tokens (hoy 700) y pedir solo los campos imprescindibles. Valorar juntar varios titulares en una sola petición. Si el usuario lo aprueba (requiere administrador), limitar la potencia de la GPU con nvidia-smi -pl. En el PC dedicado, revisar la ventilación.

## 43. La fábrica OpenClaw no reconoce qwen-trading y las dos se bloquean
*media · 24x7 · usuario · esfuerzo pequeño*

- **Evidencia:** M:\OneDrive\herramientas\OpenClaw\fabrica\lib\gpu.py:51-52 solo reconoce --alias qwen3.5-9b. Cualquier otro llama-server se trata como ajeno (gpu.py:374-378) y el turno se cancela con GPUOcupada(«otro_llm_abierto»), justo después de tomar gpu.lock (gpu.py:240-250). En M:\OpenClaw-datos\logs\crear_video.log, el 27-10 a las 17:21:30 y a las 22:19:54 aparece «llama-server ajeno abierto … no se toca» seguido de «gpu ocupada: otro_llm_abierto», y los vídeos de Pr3cise no se crearon (vigilante.json, «ultimas»). Ahora vigilante.json marca llm_ajeno=true y vram_libre_mib=1104. El bot sí respeta gpu.lock (local_ai.py:508-514 y 728-729), pero tarda hasta 30 s en verlo, y para entonces la fábrica ya ha soltado el lock. Con la cola siempre llena, el servidor del bot nunca queda libre. En la memoria del proyecto esto figura como decisión pendiente del usuario.
- **Qué falta:** Un reparto de la GPU entre el bot y la fábrica: hoy, mientras el bot corre, la fábrica no puede generar vídeo ni imagen.
- **Arreglo propuesto:** Decide el usuario. Opción mínima: en gpu.py, si el único llama-server ajeno es el de --alias qwen-trading, mantener el lock y esperar hasta 90 s a que desaparezca (el bot lo apaga al ver gpu.lock) antes de rendirse, y aplicar el mismo criterio en la condición llm_ajeno del vigilante. Otra opción es aceptar el conflicto hasta llevarse el bot al PC dedicado.

## 44. Tras un corte de internet las fuentes tardan hasta 30 min en volver y el flujo de Binance no lleva hora
*media · 24x7 · código · esfuerzo medio*

- **Evidencia:** Los reintentos existen. Precios OKX: reconexión cada 2 s, que crece hasta 120 s, y receive(timeout=25) (market.py:82-116). Libro y operaciones: igual (trend.py:452-480 y 505-523). Flujo de Binance: cada 60 s (market.py:161-199). Fuentes directas: hasta 600 s (hunters.py:198). Feeds: base×2^fallos hasta 1.800 s (feeds.py:49 y 355-358), y pausa por host de 30 s×2^k hasta 1.800 s tras 5 fallos de red (feeds.py:482-498). En cambio, no hay detección de «sin red»: en un corte, los 5.714 feeds y sus hosts acumulan fallos y pausas, y al volver la conexión tardan hasta 30 min (no hay nada en feeds.py que lo reinicie). El flujo minorista (funding, L/S) no lleva marca de hora. Si Binance cae, los valores viejos se siguen usando sin aviso en predict.py:376 y 595, apertura.py:2346, paper.py:403 y claude_agents.py:766; solo cambia ok[«flow»]. En cambio, paper descarta los precios con más de 15 s (paper.py:367-379).
- **Qué falta:** Que la recuperación tras un corte sea rápida y que no se use un dato de flujo viejo como si fuera actual.
- **Arreglo propuesto:** En FeedPoller: si en 60 s falla más del 80 % de los hosts por red, entrar en modo «sin red». En ese modo, no sumar fallos ni pausas y sondear un único host de prueba. Al volver la conexión, poner a cero fallos y pausas y reprogramar con dispersión. En Market: añadir «ts» a cada fila del flujo y que los consumidores la ignoren con más de 3-5 min de antigüedad.

## 45. La conexión es Wi-Fi por un adaptador USB con la suspensión selectiva activada
*media · 24x7 · usuario · esfuerzo pequeño*

- **Evidencia:** Get-NetAdapter: el único adaptador activo es «Wi-Fi 2», un TP-Link Wireless Nano USB Adapter (433 Mbps), con ruta por defecto a 192.168.1.1. En WLAN-AutoConfig/Operational hubo 3 desconexiones (8003) el 27-10 a las 21:20:05, 21:21:17 y 21:22:29. Según powercfg, la suspensión selectiva USB está activada con corriente (índice 1). Además, Proton VPN arranca con Windows (clave Run de HKCU), aunque ahora no está en marcha.
- **Qué falta:** Una conexión estable para el 24/7. Cada corte se suma al problema de recuperación de los feeds.
- **Arreglo propuesto:** Cable Ethernet si es posible. Si no, desactivar la suspensión selectiva USB del plan (requiere el permiso del usuario) y conectar el adaptador a un puerto trasero o con alargador. Si se usa Proton VPN, comprobar que su kill switch no corta el bot y que OKX EEA (wseea.okx.com) responde desde el país de salida.

## 46. Un fallo suelto en un bucle no protegido tumba todo el proceso
*media · 24x7 · código · esfuerzo medio*

- **Evidencia:** __main__.py:192-198 usa asyncio.gather sin return_exceptions: la primera excepción que escapa de cualquier bucle para el bot entero. Ejemplos de llamadas fuera de un try: local_ai.py:649 y 443 (bus.publish → store.add_event → INSERT + commit). Si SQLite devuelve «database is locked», el bot cae: la conexión principal usa el timeout por defecto de 5 s (store.py:32) y hay otras conexiones que escriben (store.py:129 y fuentes_vivas.py:598). report_sources (__main__.py:175-188) tampoco tiene try. En los registros actuales no aparece «locked» y la ejecución actual va sin ningún ERROR. El riesgo es plausible, no observado. Sin rearranque externo, una caída así es definitiva.
- **Qué falta:** Que cada módulo se recupere solo y que una caída del proceso no sea definitiva.
- **Arreglo propuesto:** Envolver cada tarea principal en supervise(nombre, fábrica), que registre el error y la relance con espera creciente. Dar timeout=30 a la conexión principal de Store. Mantener además el lanzador externo como última red.

## 47. Las posiciones paper abiertas durante un apagado no se reconcilian
*media · 24x7 · código · esfuerzo medio*

- **Evidencia:** Al arrancar, paper.py solo trata las órdenes pendientes que caducaron con el bot parado (paper.py:286-289 y 1207). Las posiciones abiertas siguen con el precio del momento (paper.py:1215-1220): si el stop o el objetivo se tocaron mientras el PC estaba apagado, el resultado paper no lo refleja. Con los apagados nocturnos de este PC eso afecta a los resultados y al aprendizaje.
- **Qué falta:** Reconstruir lo que pasó con cada posición mientras el bot estuvo parado.
- **Arreglo propuesto:** Al arrancar, para cada posición abierta, descargar con GET las velas de 1 min de OKX desde la última actualización y repetir stop, objetivo y funding. Si no hay velas, marcarla «con hueco» y excluirla de las métricas del aprendizaje y de la evolución.

## 48. El traslado al PC de la 3060 Ti no está preparado
*media · 24x7 · usuario · esfuerzo grande*

- **Evidencia:** Rutas fijas a M: en abrir-radar.ps1:8 y 10, arrancar-bot.ps1:3, openclaw-trading/arrancar-openclaw.ps1:3-9, openclaw-trading/instalar.py:13-19 (BASE, LLAMA, MODEL, PY y RADAR), el SKILL.md de radar y .claude/launch.json. Solo config.py las admite por variable de entorno. docs/03:84 dice «Con OneDrive llega sola», pero M:\BotFundamental-datos (bot.db de 266 MB, historico de 523 MB, backtest, estrategia_activa.json) no está en OneDrive y no hay un paso para moverlo. docs/03:92 dice de las tareas programadas «Te las preparo cuando estemos en ese PC»: no existen. fuentes_vivas escribe en bot/fuentes/<región>.json, dentro de OneDrive (fuentes_vivas.py:49 y 1287), y load_registry lee todos los *.json (feeds.py:162): con dos PCs en marcha a la vez saldrían copias en conflicto. OpenClaw: aquí está la 2026.9.6 con Node v24.21.0, y docs/03:91 pide instalar la 2026.10.6 con Node 24.22.1 o superior; nadie ha comprobado que la configuración de instalar.py sirva para esa versión. Ojo: nvidia-smi dice que ESTE PC ya tiene una RTX 3060 Ti de 8 GB.
- **Qué falta:** Un procedimiento probado para el traslado: rutas configurables, paso de datos, tareas de arranque y la regla de que el bot solo corra en un PC a la vez.
- **Arreglo propuesto:** Antes: que el usuario confirme qué PC es el dedicado, porque este también tiene una 3060 Ti. Después, preparado desde aquí: sacar las rutas a variables de entorno o a un .env en M:\BotFundamental-runtime, y escribir instalar-pc.ps1 (crear el venv y pip install -r requirements.txt con caché en M:, definir BOT_LOCAL_AI_IDLE_S=0 y BOT_LOCAL_AI_MIN_FREE_MB, crear las tareas) y mover-datos.ps1 (parar el bot, copia con la API de sqlite, robocopy de historico y backtest). Añadir un candado «un solo PC» en DATA_DIR. En el otro PC, el usuario instala Node y OpenClaw y se prueba allí.

## 57. Los cortos dependen de desbloquear X-Perps en España (KYC y test de idoneidad)
*media · externo · usuario · esfuerzo pequeño*

- **Evidencia:** Los cortos están marcados como no ejecutables hasta confirmar el acceso a X-Perps (laboratorio.py:1848 y 1865, apertura.py:1337, paper.py:650). Ahora está comprobado que hay X-Perps live de las 4 monedas (eea.okx.com/api/v5/public/instruments?instType=FUTURES, 27-10-2026) y que se ofrecen a minoristas del EEE con KYC y un test MiFID. Pero OKX avisa de que el acceso puede no estar activo en todos los países a la vez y ninguna fuente nombra España (okx.com/help/okx-x-perps-eea-regional-availability-eligibility, 04-09-2026).
- **Qué falta:** Confirmar que en la cuenta española aparece «Unlock X-Perps», pasar el test y ver el apalancamiento que OKX permite a un minorista residente en España (la CNMV, en junio de 2026, aplica a los perpetuos 2:1 en cripto). Al mismo tiempo, esto desbloquea la tabla de contado de 0,10 %.
- **Arreglo propuesto:** El usuario entra en okx.com/en-eu, va a Trade > Unlock X-Perps y comprueba si puede activarlo y qué apalancamiento máximo ve. Con eso se actualizan las marcas de «no ejecutable» y el texto de PERP_MAX_LEVERAGE (paper.py:59).

## 58. El libro público de eea.okx.com es idéntico al global; OKX dice que el EEE tiene un libro propio
*media · externo · usuario · esfuerzo pequeño*

- **Evidencia:** GET eea.okx.com y www.okx.com /api/v5/market/books para SOL-USDC y XRP-USDC el 27-10-2026 a las 23:02 UTC: 40 de 40 y 38 de 40 niveles iguales, con 250 ms de diferencia. El ticker BTC-USDC también coincide en precio, tamaños y volumen. OKX describe un «independent unified USD EEA orderbook» (okx.com/en-eu/learn/spot-margin-arrives-in-europe, modificada el 02-06-2026). El bot da por hecho que lee el libro del EEE (market.py:1 y 18, trend.py:27 y 30) y el laboratorio añade 20 pb en SOL y XRP porque «el libro EEE es menos profundo» (laboratorio.py:16 y 70).
- **Qué falta:** Saber si los precios, el diferencial y el books5 que usa el paper son los del libro donde se ejecutaría una cuenta española. Si el EEE tiene libro propio, la API pública no lo muestra y el diferencial y la profundidad del paper podrían ser mejores que los reales.
- **Arreglo propuesto:** Comparar el libro de BTC-USDC y SOL-USDC que se ve en la app o la web en-eu con la cuenta del usuario (o en el demo) con el de la API pública, o preguntar a soporte de OKX Europe. Mientras tanto, etiquetarlo como «libro público de OKX» y no como «libro del EEE».

## 8. En la web, los contadores del cazador falsean la proporción de captadas frente a ruido
*baja · vivo · código · esfuerzo pequeño*

- **Evidencia:** El snapshot del WS al conectar trae 1.139 captured y 300 filtered (config.py:27-28: HISTORY_EVENTS=1500, HISTORY_FILTERED=300), con solo 20 minutos de historia. radar.html:1701 calcula «Captadas / Pasan / Ruido» con esos eventos, así que muestra que pasa un 79 %. La realidad desde el arranque, en bot.db, es 1.420 captadas frente a 2.462 filtradas (pasa un 37 %).
- **Qué falta:** Totales reales desde el arranque o del día, no los contados en la ventana del snapshot.
- **Arreglo propuesto:** Enviar en el snapshot un mensaje con los totales por tipo (ya existen en sources_summary o events) y que la web los use.

## 9. La web da «0 medios activos» y el único medio silenciado es el resto de la prueba, atribuido a «tú, desde la web»
*baja · vivo · código · esfuerzo pequeño*

- **Evidencia:** GET /api/fuentes_vivas da medios {activa:0, prueba:0, silenciada:1, retirada:0}, mientras /api/aprendizaje da totales.medios 5.102. fuentes_vivas.py:1391 solo cuenta las filas con origen «medio», que únicamente existen tras un cambio de estado. radar.html:2517 lo pinta como «medios 0 activa…». El silenciado es medio:no-existe-prueba.invalid («Silenciada a mano: prueba», por "tú, desde la web", manual_hasta 26-11). Sale en /api/evolucion (cambios) y en /api/evolucion/historial (en evolution_log, 27-10 23:00:06). En bot.log, 23:00:06 «WebSocket rechazado: origen http://evil.example» y órdenes de prueba no válidas. Ya figura como resto conocido en la memoria del proyecto.
- **Qué falta:** Contar como activos los medios vistos sin cambio de estado y marcar el resto de prueba como tal.
- **Arreglo propuesto:** En estado(), medios activa = medios conocidos menos los de otros estados. Restaurar el medio de prueba con una acción registrada solo si el usuario lo pide (evolution_log es solo de añadir).

## 10. Metadatos de medios mal asignados en el aprendizaje (CoinDesk y Reuters salen como T3 de Corea o Japón)
*baja · vivo · código · esfuerzo pequeño*

- **Evidencia:** GET /api/aprendizaje/medio?id=coindesk.com da nivel "T3", region "kr" e idioma "ko", aunque entre sus fuentes está el feed propio «coindesk» (T2). GET /api/aprendizaje da en medios_candidatos[0] reuters.com con nivel "T3", region "jp" e idioma "ja". Esto contamina por_region, por_idioma y por_nivel.
- **Qué falta:** Que el medio herede los metadatos de su fuente directa o de la mejor, no los del primer Google News que lo trajo.
- **Arreglo propuesto:** Al rellenar medios_info, priorizar el feed directo (directo=1) y el nivel más alto. Recalcular las filas existentes.

## 11. /api/noticias: el tope de 60 deja sin efecto el parámetro horas y sin min solo devuelve el último minuto, casi sin triar
*baja · vivo · código · esfuerzo pequeño*

- **Evidencia:** server.py:24 NEWS_MAX = 60. GET /api/noticias y /api/noticias?horas=48 devuelven lo mismo (40.739 bytes cada uno): 60 noticias del último minuto, 52 sin triaje (credibilidad_ajustada null) y 12 con fresca=false. Con min=6 en 24 h salen 18 noticias, todas de la última hora (el bot venía de estar parado). La skill radar.py usa min=4 y no le afecta.
- **Qué falta:** Paginación o un límite mayor cuando se pide más tiempo, o un min por defecto mayor que 0.
- **Arreglo propuesto:** Aceptar ?limite= (hasta 500) y documentarlo en el contrato 04, o poner min=3 por defecto en la web.

## 12. El mercado mezcla dos precios por moneda y el libro de BTC en OKX EU es tan fino que el desequilibrio es ruido
*baja · vivo · código · esfuerzo pequeño*

- **Evidencia:** GET /api/tendencia (23:55:51): orderflow.coins.BTC.price 80.895,5 (operaciones de Binance en USDT, trend.py:491). A la vez, /api/estado.precios_usdc.BTC da 80.810,9 (OKX EU, USDC): un 0,10 % más. En ETH, 2.349,73 frente a 2.347,38. El desequilibrio de BTC pasa de +0,8845 (bid_vol 1,87, ask_vol 0,11) a −0,9356 (bid_vol 0,06, ask_vol 1,88) en 20 s (muestra del WS de las 23:56:11). Ese desequilibrio entra en las predicciones (flow_effect, hasta ±6 pp).
- **Qué falta:** Un único precio de referencia en USDC en toda la web y un desequilibrio con más profundidad del libro o suavizado.
- **Arreglo propuesto:** Mostrar solo el precio de OKX EU, renombrar el de Binance como «último USDT de referencia», medir el desequilibrio con más niveles o una media de 1 min, y ponderarlo menos.

## 13. La agenda macro solo trae la Fed y el BLS
*baja · vivo · código · esfuerzo pequeño*

- **Evidencia:** agenda.py:1 y 15-20: solo BLS_ICS y FOMC_DAYS. GET /api/estado.agenda_macro tiene 7 eventos, todos de la Fed o del BLS. No hay BCE, BoJ ni BEA (PIB, PCE), aunque el motor opera las aperturas de Tokio, China y Europa.
- **Qué falta:** Las reuniones del BCE y del BoJ y los datos de PIB y PCE de la BEA.
- **Arreglo propuesto:** Añadir los calendarios oficiales del BCE, el BoJ y la BEA con la misma estructura {t, name, src, impact}.

## 20. Nota pendiente 1: la web muestra la pausa y estrategia_cargada, pero params_source «evolucion» sale sin traducir y el motivo de la pausa nunca llega
*baja · codigo · código · esfuerzo pequeño*

- **Evidencia:** dashboard/radar.html:1944-1947: OPEN_SRC no tiene la clave «evolucion», así que openHead (:2014) pinta «Parámetros (evolucion): …» sin etiqueta ni explicación, y en «Versión cargada» (:2015) se omite la fuente porque OPEN_SRC[ec.fuente] no existe. :2013 lee ec.motivo, pero bot/apertura.py:1930-1931 publica estrategia_cargada sin «motivo» (solo firma, ts, pausada, fuente y registro), así que el aviso de pausa siempre sale con el texto genérico. tests/test_radar_web.py:783-784 prueba con un «motivo» que el bot no envía, y ninguna prueba usa params_source «evolucion». El campo raíz «paused» no se lee, aunque equivale a ec.pausada. La pausa tampoco se refleja en Mando ni en la recomendación de Mundo (coinSignal, :3313-3348), y limits.halt_until no se muestra.
- **Qué falta:** Que params_source y estrategia_cargada.fuente «evolucion» se vean como «Elegidos por la evolución» y que el motivo real de la pausa aparezca en el aviso.
- **Arreglo propuesto:** Añadir evolucion:["info","Evolución","elegidos por la evolución (estrategia_activa.json; solo paper)"] a OPEN_SRC; añadir motivo a self.loaded en apertura.py:1930, o que la web use O.notice; probar ambos casos en test_radar_web; y opcionalmente un aviso de «motor pausado» en Mando y Mundo.

## 22. Desde la web no se puede fijar a mano el peso de un medio, aunque los documentos dicen que sí
*baja · codigo · código · esfuerzo pequeño*

- **Evidencia:** dashboard/radar.html:2666-2676 solo envía {type:"peso_medio", peso:null}, que es «Volver al automático». Ese botón solo aparece si peso_manual es numérico (:2498), y la web no tiene ninguna forma de ponerlo, así que en la práctica nunca sale. La web tampoco envía revertir_peso. Sin embargo, docs/05 §15 dice «fijar o deshacer el peso de un medio» y el contrato §2.10 dice «Se puede fijar a mano o deshacer desde la web». El servidor sí lo admite (bot/server.py:246-260).
- **Qué falta:** Un control para fijar el peso (0 a 1,5) de un medio, o corregir los documentos.
- **Arreglo propuesto:** Añadir un campo numérico de peso con confirmación en la fila del medio (radar.html:2486-2505), o quitar «fijar» de docs/05 y del contrato.

## 24. Las pruebas corren por defecto con las reglas del usuario apagadas y la rama de reglas de la fase 2 no tiene prueba
*baja · codigo · código · esfuerzo pequeño*

- **Evidencia:** tests/conftest.py:19 pone os.environ.setdefault("BOT_USER_RULES", "0") para toda la suite. Solo test_apertura, test_paper y test_evolucion activan config.USER_RULES (18 sitios). bot/claude_agents.py:472-476 (if config.USER_RULES en make_ticket) no la ejercita ninguna prueba. No hay ninguna prueba con skip o xfail, salvo skipif sin Node (Node existe en M:\OpenClaw-runtime\node, así que corren) y un skip si no se puede crear el temporal en M:.
- **Qué falta:** Cobertura de la configuración real de producción (reglas activadas) en la fase 2, y en general.
- **Arreglo propuesto:** Pruebas de make_ticket y Strategist con monkeypatch de USER_RULES=True, y quizá una pasada de la suite con BOT_USER_RULES=1 en las pruebas no v1.

## 25. El estratega de la fase 2 no recibe la agenda macro, aunque la documentación dice que sí
*baja · codigo · código · esfuerzo pequeño*

- **Evidencia:** bot/agenda.py:1 dice «La usará el estratega en la fase 2», y docs/02-fase-1.md (Qué falta) dice «estratega (Opus 5.5), que prepara cada apertura con la agenda macro». Pero bot/claude_agents.py:805-818 (strat_user) y :1175-1187 (context) no incluyen bus.state["calendar"]. Solo la usan local_ai.py:396 (briefing) y paper.py:405 (pausa macro de 10 min).
- **Qué falta:** Que Opus sepa que dentro de su ventana hay un IPC o un FOMC, más allá de la pausa de 10 min del paper.
- **Arreglo propuesto:** Añadir «agenda»: los eventos de calendar de las próximas 24 h a strat_user, o corregir el docstring y docs/02.

## 26. Docstring desfasado en paper.py sobre funding_h
*baja · codigo · código · esfuerzo pequeño*

- **Evidencia:** bot/paper.py:12-15 dice «se da por hecho que ese periodo es de 8 h … salvo que la fila traiga funding_h (…; hoy market.py no lo envía)». bot/market.py:145-150 y :177-178 sí lo envían, y en vivo /api/estado da funding_periodo_h = 8.0 para BTC, ETH, SOL y XRP.
- **Qué falta:** Que el docstring diga que market.py publica funding_h (fundingInfo cada 6 h) y que con él se escala a 8 h.
- **Arreglo propuesto:** Reescribir las líneas 12-15 como en el contrato §1 (docs/04:64-67).

## 27. La vigilancia no está protegida: si falla al construirse, tumba todo el bot
*baja · codigo · código · esfuerzo pequeño*

- **Evidencia:** bot/__main__.py:137 crea vigil = Vigilancia(bus, store, session) sin optional(), a diferencia del aprendizaje, las fuentes vivas y la evolución (:139-157). Su __init__ hace executescript y SELECT en bot.db (vigilancia.py:519-535). El docstring de __main__.py:6 y el contrato docs/04:69 dicen que «un fallo en uno no para el resto».
- **Qué falta:** Que un error de SQLite o de datos de la vigilancia al arrancar no impida arrancar el bot.
- **Arreglo propuesto:** Envolverla en optional() y, si falta, pasar bars=CacheBars(live=lambda c: None) al aprendizaje y quitar vigil.run() y vigil.close() del gather y del finally.

## 28. Funciones definidas que nadie llama
*baja · codigo · código · esfuerzo pequeño*

- **Evidencia:** bot/aprendizaje.py:416 pesos_actuales() no tiene ninguna referencia ni en el código ni en las pruebas. bot/apertura.py:1193 recommended_params() solo se usa en tests/test_backtest_aperturas.py:430. El barrido AST de los 16 módulos clave no encontró más.
- **Qué falta:** Nada funcional.
- **Arreglo propuesto:** Borrarlas, o usar pesos_actuales en /api/aprendizaje si se quiere exponer.

## 29. Desajustes menores entre el contrato y el código
*baja · codigo · código · esfuerzo pequeño*

- **Evidencia:** (1) docs/04-contratos.md §2.9 (ejemplo de openings) pone news.label "a_favor|en_contra|sin_noticia", pero bot/apertura.py:1493-1499 devuelve «neutral», que es lo que la web espera (radar.html:1953). (2) bot/backtest.py:1155 publica un mensaje «backtest» que no está en el contrato §2 y que la web no procesa en ingest (radar.html:1172-1195); en su lugar relee /api/backtest cada 30 min (:3902). (3) Rutas que no están en el contrato §3: /health, /api/predicciones/evaluadas, /api/cartera, /api/fase2 y /api/backtest/aperturas (bot/server.py:646-658). (4) /api/fuentes_vivas sustituye el «candidatos» del estado (contadores) por una lista (server.py:465), con lo que la misma clave cambia de forma según venga del bus o de la API.
- **Qué falta:** Que el contrato sea la referencia exacta.
- **Arreglo propuesto:** Corregir sin_noticia por neutral; documentar el mensaje backtest (o hacer que la web recargue al recibirlo) y las 5 rutas; renombrar la lista a candidatos_lista o documentar la sustitución.

## 30. Rutas de la API sin ningún consumidor
*baja · codigo · código · esfuerzo medio*

- **Evidencia:** Ni dashboard/radar.html ni radar.py usan /api/aprendizaje/medios, /api/aprendizaje/estadisticas, /api/aprendizaje/postmortems, /api/aprendizaje/cambios, /api/evolucion/propuestas, /api/fuentes_vivas/historial, /api/vigilancia (su lista «operaciones», hasta 500) ni /api/predicciones/evaluadas. Hay otros campos publicados que tampoco se pintan: vigilancia vivo.por_motivo, R_total y reglas.neto_pct; predicciones accuracy.brier y by_horizon; campeon.degradado y motor.pausa_confirmada.
- **Qué falta:** O un consumidor (la web, o la skill de OpenClaw, que es el candidato natural), o asumir que son solo para depurar.
- **Arreglo propuesto:** Conectarlas en la skill radar (ver el hallazgo de OpenClaw) y las de más valor en la pestaña Evolución.

## 32. Partes de docs/02 y docs/05 están desfasadas o contradicen las reglas del usuario
*baja · codigo · código · esfuerzo pequeño*

- **Evidencia:** docs/02-fase-1.md: «Son 23 pruebas» (hoy hay 679 funciones test_ y 914 casos) y «Qué falta (fase 2)», que ya está hecho salvo la clave. docs/05 §6: «La regla es la señal → … → stop dinámico → reentradas», presentada como «tu operativa». docs/05 §8 lista break-even, stop dinámico, parcial y cierre por tiempo como salidas del paper, cuando con USER_RULES (config.py:67-69, paper.py:200-201) están desactivadas. Además, config.py:67 enuncia las reglas sin el «≥ 3 % neto» que sí aparece en el contrato :29.
- **Qué falta:** Una sola versión coherente de las reglas en los documentos.
- **Arreglo propuesto:** Marcar §6 y §8 como v2 o históricas, remitiendo a §14, y añadir el 3 % neto al comentario de config.py.

## 33. El calendario FOMC está escrito a mano y se acaba en diciembre de 2027
*baja · codigo · código · esfuerzo pequeño*

- **Evidencia:** bot/agenda.py:18-20: FOMC_DAYS termina en "2027-12-08" y el comentario dice «Las de 2027 son provisionales». Si no se actualiza, a partir de entonces no habrá pausa macro por la Fed (paper.py:405) y nada avisará de ello.
- **Qué falta:** Avisar cuando falten fechas o leerlas de la Fed.
- **Arreglo propuesto:** Registrar un aviso si la última fecha FOMC está a menos de 90 días, y revisar las de 2027 con federalreserve.gov.

## 34. El resto de la prueba del revisor anterior cuenta como un medio silenciado
*baja · codigo · usuario · esfuerzo pequeño*

- **Evidencia:** bot.db (solo lectura): source_status y source_status_log tienen 1 fila, medio:no-existe-prueba.invalid en estado silenciada, y evolution_log tiene 2 filas (la otra es el informe 2026-W43). GET /api/fuentes_vivas da medios.silenciada = 1 y la lista web la enseña como si fuera real.
- **Qué falta:** Que los contadores y la lista no incluyan esa prueba. evolution_log no se puede borrar por diseño.
- **Arreglo propuesto:** Ocultar los dominios .invalid en los contadores y en la lista de fuentes_vivas, o restaurarlo desde la web (lo que añadiría otra entrada al historial). Lo decide el usuario.

## 35. Telegram sigue sin implementar
*baja · codigo · usuario · esfuerzo medio*

- **Evidencia:** docs/02 (Qué falta) y docs/03 §4.6 lo dejan como opcional y a cargo del usuario. En bot/ y en openclaw-trading/ no hay ningún lector ni bot de Telegram (la palabra solo aparece en un comentario de fuentes_vivas.py).
- **Qué falta:** Las fuentes de Telegram y los avisos, si los sigue queriendo.
- **Arreglo propuesto:** El usuario tendría que crear el bot en @BotFather e introducir él mismo el número y el código; después se añadiría como fuente.

## 49. Procesos hijos sin Job Object: pueden quedar huérfanos
*baja · 24x7 · código · esfuerzo pequeño*

- **Evidencia:** feeds.py:619 y 654 crean un ProcessPoolExecutor de 2 procesos (lanzados con spawn), y local_ai.py:542 lanza llama-server con Popen. En bot/*.py no hay Job Object (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE) ni nada que vigile al proceso padre. Si el bot muere de golpe, los lectores se quedan bloqueados esperando su cola y llama-server sigue ocupando 5-6 GB de VRAM. Esto último está mitigado: el PID se guarda en llama-server.pid y el siguiente arranque cierra ese proceso (local_ai.py:665-700). Ahora mismo no hay ningún huérfano: 16908 y 10668 tienen parent_pid=21160, que está vivo; llama-server 8456 es hijo de 21160; ningún python, llama ni node tiene padre inexistente. Este PC tiene 11 procesos python en total, la mayoría de otras herramientas.
- **Qué falta:** Garantía de que, al morir el bot, mueran también sus hijos.
- **Arreglo propuesto:** Al arrancar el bot, crear un Job Object con KILL_ON_JOB_CLOSE (ctypes, sin dependencias) y meter en él el propio proceso: los lectores y llama-server lo heredan. Como alternativa, un hilo en cada lector que compruebe con OpenProcess si el padre sigue vivo y salga si no.

## 50. Registros: bot.log guarda solo unos 4 días y llama-server.log crece sin límite
*baja · 24x7 · código · esfuerzo pequeño*

- **Evidencia:** __main__.py:79 usa RotatingFileHandler de 5 MB con 5 copias, unos 30 MB en total. bot.log crece unos 7,3 MB al día (medido durante 60 s), así que solo cabe el historial de unos 4 días. llama-server.log se abre en modo «ab» en cada arranque (local_ai.py:541), sin rotación: crece unos 16,6 MB al día (medido), unos 6 GB al año. Ahora pesa 0,93 MB.
- **Qué falta:** Historial de registros suficiente para diagnosticar una semana de 24/7 y un tope para el registro de llama.
- **Arreglo propuesto:** Rotar bot.log a diario y guardar 30 días (TimedRotatingFileHandler), o 20 MB con 15 copias. En _ensure, renombrar llama-server.log a .1 si pasa de 50 MB, o bajar la verbosidad de llama-server.

## 51. La base crecerá a unos 5 GB el primer mes y unos 11 GB al año
*baja · 24x7 · código · esfuerzo pequeño*

- **Evidencia:** Medido en bot.db (solo lectura): unos 11.000-12.300 eventos por hora a pleno rendimiento (26-10 de 11 a 13 h, unos 11 MB/h). De ellos, unas 3.600-4.100 captadas por hora, que se guardan 30 días (config.py:33-34), aunque el comentario de config.py:31 dice que nada las lee pasadas 48 h y calcula unas 2.000 filas/h. Además: unas 14.000-15.000 claves vistas por hora de unos 123 B, durante 14 días (config.py:30); news_outcomes unas 1.083 filas/h, durante 400 días (aprendizaje.py:135); news_stories unas 244 filas/h de unos 838 B; fv_noticias unas 1.613 filas/h, durante 120 días (fuentes_vivas.py:106). La poda funciona (store.prune por lotes cada hora, __main__.py:87-100) y el WAL se mantiene en 34,3 MB. M: tiene 475 GB libres.
- **Qué falta:** Ajustar la retención a lo que de verdad se lee, para que las copias y las consultas no crezcan sin necesidad.
- **Arreglo propuesto:** Separar la retención de las captadas, a 3 días con BOT_KEEP_EVENTS_DAYS o con un KEEP propio, del resto de eventos. Actualizar el comentario de config.py:31 con la cifra real. Añadir PRAGMA optimize diario y un informe de tamaño por tabla en /health.

## 52. Quedan restos en C:, aunque el bot escribe en M:
*baja · 24x7 · código · esfuerzo pequeño*

- **Evidencia:** El bot cumple la regla: __main__.py:43-52 pone TMP, TEMP y SQLITE_TMPDIR en M:\BotFundamental-datos\tmp antes de crear los procesos hijos; la caché de pip del venv es m:\caches\pip («pip config list»); conftest.py manda las pruebas a M:. En C: hay: C:\Users\ubi_o\AppData\Local\Temp\pytest-of-ubi_o, 51,5 MB del 26-10 entre las 10:54 y las 11:01, de alguna tanda de pruebas que no pasó por conftest (pytest.ini no fija basetemp); C:\Users\ubi_o\AppData\Local\Temp\claude\M--OneDrive-Bot-de-trading-fundamental, 35 MB, que es el espacio de trabajo de las sesiones de Claude, no del bot. No son del bot: NVIDIA DXCache (1,2 GB), ComputeCache (15 MB, sin cambios desde el 06-08), .cache\huggingface (1,4 MB de 2024) y puppeteer (583 MB). No existe C:\Users\ubi_o\AppData\Local\llama.cpp ni variables HF_HOME o LLAMA_CACHE.
- **Qué falta:** Cerrar la vía por la que pytest escribió en C:.
- **Arreglo propuesto:** Añadir a pytest.ini «addopts = -p no:cacheprovider --basetemp=M:\BotFundamental-runtime\tmp\pytest» o fijar PYTEST_DEBUG_TEMPROOT como variable de usuario. Con el permiso del usuario, borrar C:\Users\ubi_o\AppData\Local\Temp\pytest-of-ubi_o. Vaciar de vez en cuando M:\BotFundamental-runtime\tmp (898 MB de restos de revisiones).

## 53. El reloj está sincronizado, pero nadie lo vigila
*baja · 24x7 · código · esfuerzo pequeño*

- **Evidencia:** «w32tm /query /status» (27-10 23:57): sincroniza con time.windows.com, capa 5, sondeo cada 1.024 s, última sincronización correcta a las 21:24:45 (2,5 h antes) y dispersión raíz de 8,47 s. Desfase medido el 28-10 con GET: Binance servidor-local = −194 ms (RTT 415 ms) y OKX −212 ms (RTT 304 ms); el reloj local va unos 0,2 s adelantado. El servicio W32Time está automático y en marcha. En bot/*.py no hay ninguna comprobación del reloj. «w32tm /query /source» dio acceso denegado porque no se ejecutó como administrador.
- **Qué falta:** Un aviso si el reloj se desvía; las aperturas y el rango de los 5 primeros minutos dependen de la hora.
- **Arreglo propuesto:** Al arrancar y cada hora, comparar con GET https://www.okx.com/api/v5/public/time y avisar en la web y en el registro si el desfase pasa de 2 s. Opcional, como administrador: añadir es.pool.ntp.org como servidor NTP.

## 54. RAM justa: 1 GB libre con el bot y la IA en marcha
*baja · 24x7 · código · esfuerzo pequeño*

- **Evidencia:** Win32_OperatingSystem: 15,9 GB en total y 1,0 GB libres; 26 GB comprometidos de 108,6 GB, con archivos de paginación en C: (14,8 GB, 875 MB en uso) y en M: (80 GB). Memoria privada: llama-server unos 7.478 MB, bot 740 MB, OneDrive 1.413 MB, Firefox unos 2,2 GB. llama-server se lanza con -c 32768 (config.py:43), aunque cada triaje usa unos 1.900 tokens (llama-server.log: n_tokens = 1901).
- **Qué falta:** Margen de memoria para usar el PC mientras corre el bot sin que se pagine.
- **Arreglo propuesto:** En este PC, bajar BOT_LOCAL_AI_CTX a 8192 cuando no se use el OpenClaw de trading, que es quien necesita los 32k. Así se libera caché KV y VRAM. En el PC dedicado no debería hacer falta.

## 55. El código del bot está en OneDrive sin «mantener siempre en este dispositivo»
*baja · 24x7 · código · esfuerzo pequeño*

- **Evidencia:** Atributos: bot\__main__.py, bot\fuentes\de.json y dashboard\radar.html tienen 0x420 (ReparsePoint, sin Pinned). En cambio, el modelo .gguf y llama-server.exe tienen 0x80420 (Pinned). Storage Sense está activado (StoragePolicy 01=1). Ningún archivo de bot\ está solo en la nube ahora mismo.
- **Qué falta:** Garantizar que, tras un reinicio sin red o sin OneDrive iniciado, el código esté en el disco.
- **Arreglo propuesto:** Con permiso del usuario: «attrib +P /S /D» en M:\OneDrive\Bot de trading fundamental, o «Mantener siempre en este dispositivo» desde el Explorador.

## 59. El funding sale de Binance, y el de X-Perps es público y ronda el doble
*baja · externo · código · esfuerzo pequeño*

- **Evidencia:** El paper y el laboratorio usan el funding de Binance USDT-M (market.py:3-5, paper.py:12-16, historia_extra.py:45). El de X-Perps se publica en eea.okx.com/api/v5/public/funding-rate(-history)?instId={moneda}-USD_UM_XPERP-310404 con el mismo horario de 8 h. Media del 24-09 al 27-10-2026: X-Perps entre 0,0068 y 0,0093 % por 8 h, Binance entre 0,0032 y 0,0037 %.
- **Qué falta:** Usar como funding de ejecución la tasa del instrumento en el que se operaría. Hoy los cortos en perpetuo cobran de menos (pesimista) y un largo en perpetuo pagaría de menos (optimista). El efecto es pequeño: unos 0,05 % en las 65 h medias.
- **Arreglo propuesto:** Leer en market.py el funding-rate de OKX X-Perps para paper.funding_8h y expected_funding_pct, y dejar el de Binance solo como señal de posicionamiento. En backtests anteriores a abril de 2026 (no hay historia de X-Perps), seguir con Binance y dejarlo marcado.

## 60. Etiquetas «sin verificar» desactualizadas en la comisión de los perpetuos
*baja · externo · código · esfuerzo pequeño*

- **Evidencia:** laboratorio.py:73 dice «0,05 % (OKX, sin verificar para X-Perps EU)» y docs/05-fuentes-tendencia-backtest-fase2.md:267 dice «no es el de OKX X-Perps EU, que está sin confirmar». La página oficial confirma 0,02 % maker y 0,05 % taker (okx.com/help/okx-x-perps-eea-fees-overview, actualizada el 01-09-2026), y el funding cada 8 h está comprobado en la API.
- **Qué falta:** Marcar estos datos como confirmados, con URL y fecha. El paper puede quedarse con 0,10 % en los perpetuos como opción prudente, pero diciéndolo.
- **Arreglo propuesto:** Actualizar el comentario de laboratorio.py:73 y el párrafo de docs/05:267 con la fuente oficial y la fecha.

## 61. El modo v2 «ruptura» compara precios de Binance en USDT con los de OKX en USDC (unos 10 pb de base)
*baja · externo · código · esfuerzo pequeño*

- **Evidencia:** apertura.py:2200-2212 (_entry_price) toma max o min entre el cierre de Binance en USDT y el precio de OKX en USDC, y compara el precio de OKX con stop_px, calculado con velas de Binance en USDT. La base medida el 27-10-2026 a las 22:57 UTC es de −8 a −11 pb (Binance USDCUSDT = 1,0010). Solo se usa en el modo «ruptura»; con las reglas del usuario activas, el motor es «impulso», que parte del precio de OKX (apertura.py:2391).
- **Qué falta:** En v2, el R:R de los largos sale unos 10 pb más prudente y el cruce del stop se detecta unos 10 pb antes de tiempo.
- **Arreglo propuesto:** En _entry_price, pasar stop_px y el cierre a la escala de OKX con la base USDC/USDT del momento, o comparar en % desde la entrada de OKX.

## Datos externos comprobados (OKX EU, comisiones, perpetuos)

Auditoría de solo lectura de los datos externos (27-28/10/2026). Solo se usaron peticiones GET públicas, fuentes oficiales de OKX, ESMA y BOE y prensa especializada. No se tocó bot.db ni ningún proceso.

1) Pares: BTC-USDC, ETH-USDC, SOL-USDC y XRP-USDC existen y están en estado «live» en la API de eea.okx.com. Comparten libro con USD, USDG y RLUSD (tradeQuoteCcyList). El tope por orden a mercado es de 25.000 USDC en SOL y de 180.000 en ETH.

2) Derivados: OKX EU ofrece X-Perps de las 4 monedas (por ejemplo BTC-USD_UM_XPERP-310404). Son futuros a 5 años con funding y liquidación en USD. Lo vende OKX Europe Markets Ltd, regulada en Malta por la MFSA, a minoristas y profesionales del EEE. Requisitos: KYC y un test de idoneidad MiFID que se hace una sola vez. OKX anuncia hasta 10x. Para el minorista español, la CNMV (junio de 2026, por una circular de CySEC) trata los perpetuos «o productos análogos» como CFD, lo que da 2:1 en cripto. No está confirmado que España esté ya habilitada ni qué apalancamiento aplica OKX a un minorista español. Alternativas para cortos: el Spot Margin de OKX EEA, solo en BTC y ETH, y Kraken, con derivados MiFID II a través de Chipre.

3) Comisiones oficiales del EEE desde el 22-04-2026: en contado, nivel Regular sin cuenta X-Perps, 0,20 % maker y 0,35 % taker. Con cuenta X-Perps, 0,08 % maker y 0,10 % taker. En X-Perps, 0,02 % maker y 0,05 % taker. El funding de X-Perps se cobra cada 8 h, a las 00:00, 08:00 y 16:00 UTC. Comprobado en 100 liquidaciones: tope de ±0,375 % en BTC y ±0,75 % en SOL. La media de los últimos 33 días es más o menos el doble que la de Binance.

4) Binance: confirmado. Los precios del paper salen del WebSocket de OKX EU (market.py:18 y 80). Las velas de 1 min (USDT), las operaciones agresivas, el funding y el ratio largo/corto salen de Binance (apertura.py:56, trend.py:28, market.py:19). config.py no menciona Binance. Foto del 27-10 a las 22:57 UTC: los precios USDC de OKX están entre 8 y 11 pb por debajo de los de Binance en USDT, por la base USDC/USDT de 1,001. Frente a Binance en USDC, la diferencia baja a entre −0,8 y +2,1 pb. El volumen de OKX en USDC es solo entre el 2 y el 3,4 % del de Binance en USDT. El diferencial de OKX va de 0,01 a 1 pb y una orden de 4.000 USDC mueve el precio como mucho unos 1,5 a 3,6 pb. El paper rellena con el precio de OKX y fija el stop en % desde ese precio, así que no le afecta. Solo el modo v2 «ruptura», inactivo con las reglas del usuario, mezcla las dos escalas.

5) Costes del proyecto: la comisión de 0,10 % por lado solo es correcta si el usuario tiene cuenta X-Perps. Sin ella es optimista: 0,35 % de taker, con un coste de ida y vuelta de unos 0,74 % frente al 0,24 % supuesto. El deslizamiento (2 pb, más medio diferencial, más 0,5-2 pb) es neutro o prudente en BTC y ETH en calma. Los extras en los stops (5 y 20 pb) son prudentes, pero no se pudo comprobar qué pasa con noticias. La comisión de los perpetuos en el paper (0,10 %) es pesimista frente al 0,05 % real. El FEE_PERP de 0,05 % del laboratorio es correcto. El funding sigue el horario correcto, pero su nivel sale de Binance: pesimista para los cortos y optimista para largos en perpetuo.

Otro hallazgo: el libro público de eea.okx.com es idéntico al global (40 de 40 niveles iguales), mientras que OKX dice que el EEE usa un libro independiente. No se puede confirmar que el paper vea el libro del EEE.

Es información, no consejo de inversión.

- **Pares al contado OKX EU:** BTC-USDC, ETH-USDC, SOL-USDC y XRP-USDC existen con state=live, quoteCcy USDC y listTime 1755763200000 (21-08-2025). Su tradeQuoteCcyList es [USDG, USD, USDC, RLUSD]: libro USD unificado. Tamaño mínimo: 0,00001 BTC, 0,001 ETH, 0,01 SOL y 1 XRP. (confirmado; https://eea.okx.com/api/v5/public/instruments?instType=SPOT 2026-10-27 22:55 UTC)
- **Tope por orden a mercado OKX EU:** maxMktAmt por orden a mercado: BTC-USDC 1.000.000, ETH-USDC 180.000, SOL-USDC 25.000 y XRP-USDC 280.000 USDC. Con 10.000 USDC y un nominal máximo del 40 % (4.000 USDC) no limita. (confirmado; https://eea.okx.com/api/v5/public/instruments?instType=SPOT 2026-10-27 22:55 UTC)
- **X-Perps: instrumentos:** Existen BTC-, ETH-, SOL- y XRP-USD_UM_XPERP-310404, con instType FUTURES, alias this_five_years, vencimiento el 04-04-2031, settleCcy USD y estado live. Contrato: 0,0001 BTC, 0,001 ETH, 0,01 SOL y 1 XRP. El campo lever (máximo técnico) es 50 en BTC, ETH y SOL y 20 en XRP. Listados desde el 30-03-2026. (confirmado; https://eea.okx.com/api/v5/public/instruments?instType=FUTURES 2026-10-27 22:56 UTC)
- **X-Perps: quién puede acceder:** Hace falta cuenta verificada (KYC) y un test de idoneidad MiFID que se hace una sola vez; si se suspende, hay un tiempo de espera. Pueden acceder minoristas y profesionales, con la misma oferta. Los 30 países del EEE están soportados, pero el acceso puede no estar activo a la vez en todos. (confirmado; https://www.okx.com/help/okx-x-perps-eea-regional-availability-eligibility 2026-09-04)
- **X-Perps: España habilitada:** Ninguna fuente oficial nombra España. La página de OKX dice que la activación por país puede ir con retraso y remite a comprobarlo en la app o en la web. (SIN CONFIRMAR; https://www.okx.com/help/okx-x-perps-eea-regional-availability-eligibility 2026-09-04)
- **X-Perps: apalancamiento anunciado:** OKX anuncia hasta 10x y no separa minoristas de profesionales. Colateral admitido: BTC, ETH, SOL, USDC, USDG y DOGE. Liquidación en efectivo a 5 años. Lo ofrece OKX Europe Markets Ltd, regulada por la MFSA. (confirmado; https://www.okx.com/learn/what-are-x-perps-guide-for-european-traders 2026-08-14)
- **ESMA: perpetuos como CFD:** Declaración ESMA35-243228190-8024: los derivados vendidos como «perpetual futures» probablemente entran en las medidas nacionales sobre CFD (límite de apalancamiento, cierre por margen, protección de saldo negativo). El nombre comercial no importa. Exige test de idoneidad y KID PRIIPs. Ojo: la definición de CFD excluye los «future», y los X-Perps vencen a 5 años. (confirmado; https://www.esma.europa.eu/sites/default/files/2026-02/ESMA35-243228190-8024_-_Public_statement_on_derivatives_in_scope_of_the_CFD_product_intervention_measures.pdf 2026-02-24)
- **CNMV: perpetuos para minoristas españoles:** Según una circular de CySEC que recoge la postura de la CNMV, los futuros referenciados al contado y los «perpetual futures, or analogue products» cuentan como CFD para el minorista español. Les aplican las resoluciones de 2019 (apalancamiento 2:1 en cripto) y de julio de 2023 (prohibición de publicidad). Fuente secundaria. (confirmado; https://www.financemagnates.com/forex/regulation/spain-treats-spot-quoted-and-perpetual-futures-as-cfds-in-notice-to-cyprus-brokers/ 2026-06-10)
- **CNMV: resolución sobre CFD:** Resolución de la CNMV de 11-07-2023 (BOE 14-07-2023): prohíbe la publicidad de CFD a minoristas y obliga a cerrar la posición cuando los fondos caen por debajo del 50 % del margen inicial. Se remite a la resolución de 2019, que fija 1/2 en criptoactivos. (confirmado; https://www.boe.es/diario_boe/txt.php?id=BOE-A-2023-16394 2023-07-14)
- **Apalancamiento real de OKX para un minorista español:** No se sabe si OKX limita los X-Perps a 2:1 para minoristas residentes en España (CNMV) o permite hasta 10x. El tope de 2:1 del paper (paper.py:59) es prudente y casi nunca se alcanza: con un riesgo del 0,5 % y un stop de al menos 1,5 %, el nominal es como mucho el 33 % del capital. (SIN CONFIRMAR; https://www.okx.com/learn/how-to-start-trading-x-perps-on-okx 2026-10-28)
- **Comisiones de contado EEE (sin cuenta X-Perps):** Desde el 22-04-2026: Regular 0,200 % maker y 0,350 % taker. VIP1 (100.000 EUR en activos o volumen) 0,100 % y 0,200 %. Hasta VIP9: 0,030 % y 0,055 %. Pueden revisarse cada 3 meses por grupos de tarifa. (confirmado; https://www.okx.com/help/notice-updates-to-okx-eea-trading-fees 2026-04-08)
- **Comisiones de contado EEE (con cuenta X-Perps):** Regular (0-100.000 EUR en activos o 0-1 M EUR de volumen a 30 días): 0,0800 % maker y 0,1000 % taker. VIP1: 0,0675 % y 0,0800 %. La página no detalla qué condición exacta da derecho a esta tabla. (confirmado; https://www.okx.com/help/eea-xperp-spot-trading-fees-comparison 2026-10-19)
- **Tarifa por par concreto:** OKX menciona grupos de tarifa por par y avisa de que las comisiones varían por región. No se ha podido confirmar la tarifa exacta de BTC/ETH/SOL/XRP-USDC: solo se ve en la cuenta del usuario. (SIN CONFIRMAR; https://www.okx.com/help/notice-updates-to-okx-eea-trading-fees 2026-04-08)
- **Comisiones X-Perps:** Base: 0,02 % maker y 0,05 % taker. Con mucho volumen, el maker pasa a −0,01 % y el taker a unos 0,015 %. El funding es un pago entre traders, no una comisión de OKX. (confirmado; https://www.okx.com/help/okx-x-perps-eea-fees-overview 2026-09-01)
- **Funding de X-Perps: mecánica:** Liquidación cada 8 h a las 00:00, 08:00 y 16:00 UTC: en 100 periodos de cada moneda, todos los saltos son de 8 h. formulaType withRate, interestRate 0,01 % por periodo. Tope de ±0,375 % en BTC y ±0,75 % en SOL. Próxima liquidación el 28-10 a las 00:00 UTC, con tasa de 0,01 % en BTC. (confirmado; https://eea.okx.com/api/v5/public/funding-rate-history?instId=BTC-USD_UM_XPERP-310404&limit=100 2026-10-27 22:56 UTC)
- **Funding de X-Perps frente a Binance:** Media por periodo de 8 h del 24-09 al 27-10-2026. X-Perps: BTC 0,0068 %, ETH 0,0084 %, SOL 0,0082 % y XRP 0,0093 %. Binance USDT-M: 0,0036 %, 0,0037 %, 0,0032 % y 0,0034 %. X-Perps paga más o menos el doble, y Binance también liquida cada 8 h. (confirmado; https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=100 2026-10-27 22:56 UTC)
- **Qué datos de Binance usa el bot:** Los precios del paper vienen de OKX EU por WebSocket (wss://wseea.okx.com, tickers {moneda}-USDC; market.py:18 y 80). De Binance salen el posicionamiento y el funding (market.py:19), las velas de 1 min en USDT de las aperturas y la vigilancia (apertura.py:56 y 223), las operaciones agresivas (trend.py:28) y la historia de los backtests (laboratorio.py:325, backtest_aperturas.py:55). config.py no tiene ninguna referencia a Binance: solo COINS, QUOTE=USDC y FEE_PCT=0.10 (config.py:21, 22 y 63). (confirmado; M:\OneDrive\Bot de trading fundamental\bot\market.py 2026-10-28)
- **Diferencia de precio OKX EU frente a Binance:** Precio medio de OKX en USDC frente a Binance en USDT: BTC −10,0 pb, ETH −9,1, SOL −7,9 y XRP −11,3 (en Binance, USDCUSDT = 1,0010). Frente a Binance en USDC: −0,4, +2,1, +2,0 y −0,8 pb. La diferencia es casi toda la base USDC/USDT y no afecta a los rendimientos en %. (confirmado; https://eea.okx.com/api/v5/market/ticker?instId=BTC-USDC 2026-10-27 22:57 UTC)
- **Volumen OKX EU frente a Binance:** Volumen en 24 h. OKX en USDC: BTC 29,1 M, ETH 14,2 M, SOL 2,79 M y XRP 3,24 M. Binance en USDT: 1.062 M, 485 M, 134,5 M y 94,8 M. OKX es solo entre el 2,1 y el 3,4 % de Binance. En X-Perps, BTC mueve unos 220 M USD y SOL unos 10,8 M. (confirmado; https://data-api.binance.vision/api/v3/ticker/24hr?symbol=BTCUSDT 2026-10-27 22:57 UTC)
- **Diferencial y profundidad en OKX EU:** Diferencial: BTC 0,01 pb, ETH 0,04, SOL 0,99 y XRP 0,75 pb. Impacto de 4.000 USDC frente al precio medio: compra hasta 1,5 pb (XRP) y venta hasta unos 3,6 pb (ETH y SOL). Con 50.000 USDC, hasta 5,7-6,6 pb. Foto en hora tranquila, con el ticker y el libro tomados con unos cientos de ms de diferencia. (confirmado; https://eea.okx.com/api/v5/market/books?instId=SOL-USDC&sz=400 2026-10-27 22:57 UTC)
- **Libro del EEE frente al global:** El libro público de eea.okx.com coincide con el de www.okx.com: 40 de 40 niveles en SOL-USDC y 38 de 40 en XRP-USDC, con 250 ms de diferencia. Sin embargo, OKX dice que el EEE opera en un «independent unified USD EEA orderbook». No se puede confirmar que el bot lea el libro en el que se ejecutaría. (SIN CONFIRMAR; https://www.okx.com/en-eu/learn/spot-margin-arrives-in-europe 2026-10-27 23:02 UTC)
- **Base del X-Perp frente al contado:** Precio medio del X-Perp frente al contado USDC: BTC +1,1 pb, ETH −3,9, SOL −4,0 y XRP −1,5 pb. El paper rellena los perpetuos al precio de contado de OKX: error de pocos pb. (confirmado; https://eea.okx.com/api/v5/market/ticker?instId=BTC-USD_UM_XPERP-310404 2026-10-27 22:57 UTC)
- **Coste del proyecto: comisión de contado:** FEE_PCT = 0,10 % por lado (config.py:63), en el paper (paper.py:971, 1100 y 1122), en las aperturas (apertura.py:759) y en el laboratorio (laboratorio.py:67). Coincide con el taker del nivel Regular con cuenta X-Perps. Sin esa cuenta, el taker es 0,35 %: el proyecto sería optimista unas 3,5 veces. (confirmado; https://www.okx.com/help/eea-xperp-spot-trading-fees-comparison 2026-10-28)
- **Coste del proyecto: deslizamiento:** COST_SLIP_BPS = 2 pb por lado y diferencial supuesto de 2 pb si no hay dato (paper.py:37 y 52). El paper simula medio diferencial más 0,5-2 pb (paper.py:239 y 961). Coste de ida y vuelta: 0,24 % (paper.py:169). Medido: diferencial de 0,01-1 pb e impacto de 4.000 USDC de 0,4-3,6 pb. Es neutro o prudente en calma; con noticias no se ha medido. (SIN CONFIRMAR; https://eea.okx.com/api/v5/market/books?instId=BTC-USDC&sz=400 2026-10-27 22:57 UTC)
- **Coste del proyecto: extras en los stops del laboratorio:** STOP_EXTRA: 5 pb en BTC y ETH y 20 pb en SOL y XRP. TP_THROUGH: 2 pb (laboratorio.py:69-72). Comparado con un impacto medido de 0,4-6,6 pb hasta 50.000 USDC, es prudente en calma. Sin dato en momentos de estrés. (SIN CONFIRMAR; https://eea.okx.com/api/v5/market/books?instId=XRP-USDC&sz=400 2026-10-27 22:57 UTC)
- **Coste del proyecto: comisión de los perpetuos:** El laboratorio usa FEE_PERP = 0,05 % (laboratorio.py:73), que coincide con el taker de X-Perps: correcto. El paper cobra 0,10 % por lado también en los perpetuos (paper.py:971): pesimista en 0,10 % de ida y vuelta, ya documentado como prudente en docs/05:267. (confirmado; https://www.okx.com/help/okx-x-perps-eea-fees-overview 2026-09-01)
- **Coste del proyecto: funding:** Usa el lastFundingRate de Binance escalado a 8 h (market.py:3-5, paper.py:12-16), con liquidaciones a las 00, 08 y 16 UTC (paper.py:60). El horario coincide con X-Perps. El nivel es más o menos la mitad: pesimista para los cortos, que cobran de menos, y optimista para largos en perpetuo. expected_funding_pct (apertura.py:1566) solo cuenta los pagos: prudente. Ahora el bot publica funding_pct_8h de BTC = 0,0069 (GET /api/estado), frente al 0,01 % de X-Perps. (confirmado; https://eea.okx.com/api/v5/public/funding-rate?instId=BTC-USD_UM_XPERP-310404 2026-10-28 00:02 Madrid)
- **Alternativa para cortos: Spot Margin de OKX EEA:** Lanzado el 16-12-2025: pares de BTC, ETH y USDC, largos y cortos hasta 10×, margen cruzado, en un libro USD independiente del EEE. No incluye SOL ni XRP. No se ha confirmado qué requisitos aplica a minoristas ni el coste de los intereses. (confirmado; https://www.okx.com/en-eu/learn/spot-margin-arrives-in-europe 2026-06-02)
- **Alternativa para cortos: Kraken:** Desde mayo de 2025, Kraken ofrece en el EEE perpetuos y futuros con vencimiento bajo MiFID II, a través de una empresa de inversión chipriota con licencia de CySEC. Hay que pasar una evaluación para desbloquearlos. No se ha confirmado el apalancamiento para un minorista español. (confirmado; https://www.businesswire.com/news/home/20250520169459/en/Kraken-Launches-Europes-Largest-Regulated-Futures-Offering-Strengthening-Market-Leading-Position-in-the-Region 2025-05-20)
