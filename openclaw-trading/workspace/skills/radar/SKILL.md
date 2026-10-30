---
name: radar
description: Lee el Radar Fundamental (precios USDC, noticias puntuadas, balanza de noticias, cómo reacciona la cripto en cada apertura de bolsa, libro de órdenes y volumen, predicciones de cripto, la estrategia activa y el motor de aperturas, backtest y laboratorio, vigilancia, aprendizaje de medios y post-mortems, evolución, fuentes vivas, noticias fiables y cartera paper). Úsala para cualquier pregunta sobre el mercado cripto, las aperturas de bolsa o lo que mide el bot.
metadata: { "openclaw": { "os": ["win32"] } }
---
# Radar de mercado

Ejecuta con la herramienta exec exactamente uno de estos comandos y responde a partir de su salida.
Todos empiezan por `M:/BotFundamental-runtime/venv/Scripts/python.exe -I -B M:\BotFundamental-openclaw\workspace\skills\radar\scripts\radar.py` seguido de:

- `estado`: precios, flujo minorista, agenda, fuentes e IA local.
- `noticias --horas 3 --min 5`: noticias relevantes de las últimas N horas (1-48) con importancia mínima M (0-10). Añade `--bolsa tokio` (o seul, shanghai, hongkong, dubai, frankfurt, londres, madrid, nuevayork, saopaulo) para las que afectan a esa bolsa.
- `balanza`: hacia dónde empujan las noticias de las últimas 6 h en cada moneda (compra frente a venta) y las que más pesan. Cuenta lo mismo que el motor: no pesan las de fuentes silenciadas o retiradas ni las de credibilidad ajustada 0 (macro sin reflejo confirmado en la cripto).
- `tendencia`: cómo se mueven BTC, ETH, SOL y XRP en cada apertura de bolsa (en curso, a las 4 h o la anterior), su histórico y el encadenamiento entre aperturas (que no predice), más el libro de órdenes, compras agresivas, CVD y volumen relativo de cada moneda. Nunca el índice: al usuario solo le importa la cripto.
- `predicciones`: probabilidad de subida de la cripto en las próximas ventanas, con sus motivos y el acierto medido.
- `estrategia`: qué estrategia opera ahora el motor en la cartera paper («6 diarias» o E2 · impulso), de dónde salen sus parámetros, si está pausada, sus reglas, los topes del día y lo que dice su backtest.
- `aperturas`: el motor en cada apertura: fase, sentido del impulso, instrumento (contado o perpetuo), volumen relativo, entradas y plan (entrada, stop, objetivo y neto tras costes). Horquilla pura: sin break-even ni stop dinámico.
- `backtest`: el backtest del motor (la estrategia en marcha, «6 diarias» y E2), el laboratorio de 2.000 estrategias y las estrategias generales. Ninguna tiene ventaja robusta fuera de muestra.
- `vigilancia`: la prueba en avance de las estrategias del laboratorio con operaciones virtuales (sin dinero) frente a su backtest.
- `aprendizaje`: acierto de las noticias ajustado al mercado, ranking de medios con su peso y los últimos post-mortems. `aprendizaje --por region` (o nivel, idioma, tipo, ventana, fase, rol, moneda): el acierto por esa dimensión.
- `postmortems`: explicación de las últimas operaciones cerradas. `postmortems --origen paper` (o vigilancia) para ver solo unas.
- `evolucion`: el campeón (la estrategia del motor) y sus aspirantes, las propuestas pendientes y el informe semanal.
- `propuestas`: las propuestas de la evolución. `propuestas --estado pendiente` (o aplicada, descartada, caducada, revertida) para ver solo unas.
- `fuentes`: cuántas fuentes están activas, en prueba, silenciadas o retiradas, cuáles están silenciadas, los medios candidatos a feed propio y los últimos cambios.
- `fiables`: noticias fiables: cuántos grupos pasan el criterio, cuántos pasan la puerta para operar, cuántos pasarían por azar y la nota.
- `cartera`: capital, resultado del día y posiciones de la cartera paper (dinero ficticio).
- `flujo` y `agenda`: solo el flujo minorista o solo la agenda macro.

No cambies las rutas ni añadas otros argumentos: la lista de comandos permitidos los bloquearía.
Todo es paper (dinero ficticio). Da las cifras tal cual, también cuando pierden: por ejemplo, «6 diarias» pierde en el backtest (−0,12 R por operación fuera de muestra). No prometas rentabilidad ni des consejos de inversión.
Si la salida empieza por `ERROR`, explica que el bot no responde y que hay que arrancarlo con `arrancar-bot.ps1`.
